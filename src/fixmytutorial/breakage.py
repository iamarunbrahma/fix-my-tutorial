"""When did it break? Find the package release that broke a tutorial step.

The cited sources suggest a release (Nemotron 3.5 Lightning reads them, and we only accept a version that is a real
release and appears in the quoted text). Runs from the saved checkpoint decide: the step runs once with the release
before and once with the suggested one, then a short binary search if the suggestion was wrong. Old releases often
can't install on today's Python; then the answer rests on the cited source alone, and says so.

Best effort: an error, a timeout or a budget stop leaves the answer empty. It never changes a run's verdict.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from dataclasses import dataclass
from datetime import date
from typing import Awaitable, Callable
from urllib.parse import quote

import httpx
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel

from .executor import Trace, group_units, is_install
from .ingest import USER_AGENT
from .models import BreakGuess, BreakPoint, Edit, Evidence, FailureSignature, RunPlan
from .prompts import BREAK_SYSTEM
from .repair import apply_edits
from .triage import failure_output, key_line

log = logging.getLogger("fixmytutorial.breakage")
ELIGIBLE = {"removed_api", "renamed_package", "changed_cli", "version_conflict"}
PY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]{0,213}$")
FREEZE_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)\s*$")
NPM_LS_LINE = re.compile(r"(?:├──|└──|\+--|`--)\s+(@?[^@\s]+)@(\S+)")
Probe = Callable[[str], Awaitable[str]]  # version -> "pass" | "same" | "other" | "no_install"


class Release(BaseModel):
    version: str
    date: date


@dataclass
class BreakTarget:
    """The first failure worth dating, as it was when the step broke."""
    plan: RunPlan
    trace: Trace | None  # its states lead up to the failing unit
    step_id: str
    signature: FailureSignature
    evidence: list[Evidence]
    versions: str  # `pip list --format=freeze` or `npm ls` from the failing state


def norm(language: str, name: str) -> str:
    name = (name or "").strip()
    return re.sub(r"[-_.]+", "-", name).lower() if language == "python" else name.lower()


def _version(v: str | None) -> Version | None:
    try:
        return Version(v or "")
    except InvalidVersion:
        return None


def installed_versions(language: str, text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if language == "python":
        for line in (text or "").splitlines():
            m = FREEZE_LINE.match(line)
            if m:
                out[norm(language, m.group(1))] = m.group(2)
    else:
        for m in NPM_LS_LINE.finditer(text or ""):
            out[norm(language, m.group(1))] = m.group(2)
    return out


def eligible(plan: RunPlan, trace: Trace, sig: FailureSignature) -> bool:
    """A package change broke a step that runs after the installs, so another release can be tried before it."""
    if sig.category not in ELIGIBLE or not trace.failed_unit or trace.failing_step_id is None:
        return False
    step = next((s for s in plan.steps if s.id == trace.failing_step_id), None)
    return step is not None and not is_install(step)


class Registry:
    """Release dates from PyPI and npm: fixed public hosts, read only."""

    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http

    async def releases(self, language: str, package: str) -> list[Release]:
        if language == "python":
            if not PY_NAME.match(package):
                return []
            url = f"https://pypi.org/pypi/{package}/json"
        else:
            if not NPM_NAME.match(package):
                return []
            url = f"https://registry.npmjs.org/{quote(package, safe='@')}"
        http = self._http or httpx.AsyncClient(timeout=15, follow_redirects=True,
                                               headers={"User-Agent": USER_AGENT})
        try:
            r = await http.get(url, timeout=15)
        finally:
            if self._http is None:
                await http.aclose()
        if r.status_code != 200:
            return []
        data = r.json()
        out: list[Release] = []
        if language == "python":
            for v, files in (data.get("releases") or {}).items():
                ver = _version(v)
                days = sorted(f.get("upload_time_iso_8601", "")[:10] for f in files or [] if not f.get("yanked"))
                if ver is None or ver.is_prerelease or ver.is_devrelease or not days or not days[0]:
                    continue
                out.append(Release(version=v, date=date.fromisoformat(days[0])))
        else:
            times = data.get("time") or {}
            for v in data.get("versions") or {}:
                ver = _version(v)
                if "-" in v or ver is None or v not in times:
                    continue
                out.append(Release(version=v, date=date.fromisoformat(times[v][:10])))
        return sorted(out, key=lambda r: Version(r.version))


def _mentions(quote_text: str, *versions: str) -> bool:
    """The quote states this exact version (so "2.0" does not match "2.0.5" or "12.0")."""
    return any(re.search(rf"(?<![\d.]){re.escape(v)}(?!\.?\d)", quote_text or "") for v in versions if v)


def _series_start(rel: list[Release]) -> int:
    """Without a cited version, try the first release of the installed major series (minor series for 0.x)."""
    cur = Version(rel[-1].version)
    for i, r in enumerate(rel):
        v = Version(r.version)
        if (v.major == cur.major) and (cur.major > 0 or v.minor == cur.minor):
            return i
    return len(rel) - 1


def _same_error(line: str, sig: FailureSignature, original: str = "") -> bool:
    """The probe failed the way the run did: same error line as the run's output (or the triage copy of it), or
    the same failing symbol."""
    if line.strip() and line.strip() in {original.strip(), (sig.key_line or "").strip()}:
        return True
    last = (sig.symbol or "").split(".")[-1]
    return bool(last) and last in line


async def _guess(llm, target: BreakTarget, installed: dict[str, str], run_id: str, public: bool) -> BreakGuess | None:
    if not target.evidence:
        return None  # no sources: nothing to read a version from
    user = json.dumps({
        "error": target.signature.key_line, "symbol": target.signature.symbol, "category": target.signature.category,
        "installed": [f"{k}=={v}" for k, v in list(installed.items())[:150]],
        "sources": [{"id": e.id, "title": e.title, "quote": e.quote[:700]} for e in target.evidence[:6]]})
    try:
        return await llm.call("triage", BREAK_SYSTEM, user, BreakGuess, run_id=run_id, public=public,
                              max_tokens=300, temperature=0)
    except Exception:
        return None


def make_probe(executor, target: BreakTarget, language: str, package: str, *, run_id: str, public: bool) -> Probe:
    """Run the failing step with one release of the package, starting from the checkpoint before it."""
    trace = target.trace
    assert trace is not None and trace.failed_unit, "make_probe needs the trace the step failed in"
    fu = trace.failed_unit
    prev = group_units(target.plan.steps)[fu - 1][-1]
    first = trace.results.get(target.step_id)
    original = key_line(failure_output(first)) if first else ""

    async def probe(version: str) -> str:
        spec = f"{package}=={version}" if language == "python" else f"{package}@{version}"
        cmd = (f"pip install -q {shlex.quote(spec)}" if language == "python"
               else f"npm install --no-save --no-audit --no-fund {shlex.quote(spec)}")
        plan = apply_edits(target.plan, [Edit(op="insert_after", step_id=prev.id, new_content=cmd, new_kind="shell",
                                              reason="Find the release that broke this step")])
        t = await executor.execute(plan, trace.states, from_unit=fu, stop_after=fu + 1, run_id=run_id,
                                   public=public, quiet=True)
        if t.failed_unit == fu:
            return "no_install"
        if t.failed_unit is None:
            return "pass"
        r = t.results.get(target.step_id)
        if t.failing_step_id != target.step_id or r is None:
            return "other"
        return "same" if _same_error(key_line(failure_output(r)), target.signature, original) else "other"

    return probe


async def find_break(target: BreakTarget, *, language: str, llm, registry, probe_for: Callable[[str], Probe],
                     max_probes: int, time_s: float, run_id: str, public: bool) -> BreakPoint | None:
    found: dict = {"notes": None}  # the release-notes answer, kept if the runs can't finish in time
    try:
        async with asyncio.timeout(time_s):
            return await _find(target, language, llm, registry, probe_for, max_probes, run_id, public, found)
    except Exception as e:  # timeouts, budget stops, registry and sandbox errors: the run carries on without it
        log.info("break finder stopped: %r", e)
        return found["notes"]


async def _find(target, language, llm, registry, probe_for, max_probes, run_id, public, found) -> BreakPoint | None:
    installed = installed_versions(language, target.versions)
    guess = await _guess(llm, target, installed, run_id, public)
    pkg = next((norm(language, c) for c in ([guess.package] if guess else []) + [target.signature.package or ""]
                if norm(language, c) in installed), None)
    if pkg is None:
        return None
    current = _version(installed[pkg])
    rel = [r for r in await registry.releases(language, pkg) if current and (v := _version(r.version)) and v <= current]
    if not rel or Version(rel[-1].version) != current:
        return None  # the installed version isn't a published release; nothing to compare against

    notes_idx = None
    if guess and guess.version and norm(language, guess.package) == pkg:
        gv = _version(guess.version)
        idx = next((i for i, r in enumerate(rel) if gv is not None and Version(r.version) == gv), None)
        ev = next((e for e in target.evidence if e.id == guess.evidence_id), None)
        if idx is not None and ev is not None and _mentions(ev.quote, guess.version, rel[idx].version):
            notes_idx = idx

    def notes_answer(last_good: Release | None = None) -> BreakPoint | None:
        if notes_idx is None:
            return None
        r = rel[notes_idx]
        return BreakPoint(package=pkg, step_id=target.step_id, first_bad=r.version, first_bad_date=r.date,
                          last_good=last_good.version if last_good else None,
                          last_good_date=last_good.date if last_good else None, how="release_notes",
                          evidence_id=guess.evidence_id if guess else None)

    def ran(lo: int, hi: int) -> BreakPoint:
        return BreakPoint(package=pkg, step_id=target.step_id, first_bad=rel[hi].version, first_bad_date=rel[hi].date,
                          last_good=rel[lo].version, last_good_date=rel[lo].date, how="ran",
                          evidence_id=guess.evidence_id if guess and notes_idx == hi else None)

    found["notes"] = notes_answer()
    idx = notes_idx if notes_idx is not None else _series_start(rel)
    if idx == 0:
        return found["notes"]  # no earlier release to run
    probe = probe_for(pkg)
    bad = len(rel) - 1  # the installed release is the one that failed in the run
    if idx == bad:
        before, at, used = await probe(rel[idx - 1].version), "same", 1
    else:
        before, at = await asyncio.gather(probe(rel[idx - 1].version), probe(rel[idx].version))
        used = 2
    if before in ("no_install", "other"):  # the older release can't be run here; only the source can say
        if at == "pass":
            found["notes"] = None
        return found["notes"]
    if before == "same":  # the release before the named one fails the same way: the sources don't explain it
        found["notes"] = None
        return None
    if at == "same":
        return ran(idx - 1, idx)
    if at in ("no_install", "other"):
        return notes_answer(last_good=rel[idx - 1])
    found["notes"] = None  # the named release still works: the change came later
    lo, hi = idx, bad
    while hi - lo > 1 and used < max_probes:
        mid = (lo + hi) // 2
        r = await probe(rel[mid].version)
        used += 1
        if r == "pass":
            lo = mid
        elif r == "same":
            hi = mid
        else:
            return None
    return ran(lo, hi) if hi - lo == 1 else None
