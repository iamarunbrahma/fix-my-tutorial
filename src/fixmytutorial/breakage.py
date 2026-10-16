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
from urllib.parse import quote, urlparse

import httpx
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel

from .executor import Trace, group_units, is_install
from .ledger import BudgetExceeded
from .ingest import USER_AGENT
from .models import BreakGuess, BreakPoint, Edit, Evidence, FailureSignature, RunPlan
from .prompts import BREAK_SYSTEM
from .research import MARKER_RE
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
        http = self._http or httpx.AsyncClient(
            timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
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
                days = sorted(
                    f.get("upload_time_iso_8601", "")[:10] for f in files or [] if not f.get("yanked")
                )
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


VERSION_RE = re.compile(r"(?<![\d.])\d+\.\d+(?:\.\d+)?(?!\.?\d)")
CHANGE_WORDS = ("removed", "expired", "dropped", "no longer", "renamed", "replaced", "breaking", "esm")


def _mentions(text: str, *versions: str) -> bool:
    """The text states this exact version: dotted, and not part of another one ("2.0" is not in "2.0.5", "12.0" or
    "2.0rc1"; a bare "2" states nothing)."""
    return any(re.search(rf"(?<![\d.]){re.escape(v)}(?!\.?\d|\.?[A-Za-z])", text or "")
               for v in versions if v and "." in v)


def _address(url: str) -> str:
    """A release note's path without its file extension, so ".../v2.0.0.html" states 2.0.0."""
    return re.sub(r"\.(?:html?|md|rst|txt)$", "", urlparse(url).path)


def _series_start(rel: list[Release], i: int) -> int:
    """First release of the series rel[i] belongs to: same major version (same minor for 0.x)."""
    cur = Version(rel[i].version)
    for j, r in enumerate(rel[: i + 1]):
        v = Version(r.version)
        if v.major == cur.major and (cur.major > 0 or v.minor == cur.minor):
            return j
    return i


def _same_error(line: str, sig: FailureSignature, original: str = "") -> bool:
    """The probe failed the way the run did: same error line as the run's output (or the triage copy of it), or
    the same failing symbol."""
    if line.strip() and line.strip() in {original.strip(), (sig.key_line or "").strip()}:
        return True
    last = (sig.symbol or "").split(".")[-1]
    return bool(last) and last in line


def _package_in_step(plan: RunPlan, step_id: str, sig: FailureSignature, installed: dict[str, str]) -> str | None:
    """When nobody named the package: the installed package the failing step's own code names, if only one does."""
    step = next((s for s in plan.steps if s.id == step_id), None)
    if step is None:
        return None
    # the step's own code, plus files it runs ("node index.js" runs the index.js an earlier step wrote)
    code = step.content + "".join("\n" + s.content for s in plan.steps
                                  if s.kind == "write_file" and s.path and s.path in step.content)

    def named(text: str, name: str) -> bool:
        return any(re.search(rf"(?<![\w-]){re.escape(v)}(?![\w-])", text, re.I) for v in {name, name.replace("-", "_")})

    hits = [n for n in installed if named(code, n)]
    if len(hits) > 1:
        hits = [n for n in hits if named(sig.key_line or "", n)] or hits
    return hits[0] if len(hits) == 1 else None


async def _guess(llm, target: BreakTarget, evidence: list[Evidence], installed: dict[str, str], run_id: str,
                 public: bool) -> BreakGuess | None:
    if not evidence:
        return None  # no sources: nothing to read a version from
    user = json.dumps({
        "error": target.signature.key_line, "symbol": target.signature.symbol, "category": target.signature.category,
        "installed": [f"{k}=={v}" for k, v in list(installed.items())[:150]],
        "sources": [{"id": e.id, "url": e.url, "title": e.title, "quote": e.quote[:700]} for e in evidence[:6]]})
    try:
        return await llm.call("triage", BREAK_SYSTEM, user, BreakGuess, run_id=run_id, public=public,
                              max_tokens=1500, temperature=0)
    except Exception:
        return None


def _version_passage(text: str, term: str) -> str:
    """The paragraph of a release note that names the symbol, preferring one that states a version and a change."""
    paras = [re.sub(r"\s+", " ", MARKER_RE.sub("", p)).strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    if term:
        paras = [p for p in paras if term.lower() in p.lower()]
    if not paras:
        return ""

    def score(p: str) -> float:
        low = p.lower()
        return 2 * bool(VERSION_RE.search(p)) + any(w in low for w in CHANGE_WORDS) - len(p) / 5000

    return max(paras, key=score)[:600]


async def _release_notes(search, pkg: str, sig: FailureSignature, run_id: str, public: bool) -> list[Evidence]:
    """One search for the release note that says in which version the failing thing changed (about 2 credits)."""
    term = (sig.symbol or "").split(".")[-1]
    query = (f"{pkg} {term} removed in which version: {sig.key_line[:150]}" if term
             else f"{pkg} release notes breaking change: {sig.key_line[:150]}")
    results = [r for r in await search.search(query, run_id=run_id, public=public)
               if urlparse(r.get("url", "")).scheme in ("http", "https")][:2]
    if not results:
        return []
    ex = await search.extract([r["url"] for r in results], run_id=run_id, public=public,
                              query=f"{pkg} {term} removed version".strip())
    texts = {e["url"]: e["raw_content"] for e in ex}
    items: list[Evidence] = []
    for r in results:
        passage = _version_passage(texts.get(r["url"]) or r.get("content") or "", term)
        if passage:
            items.append(Evidence(id=f"b{len(items) + 1}", url=r["url"], title=(r.get("title") or r["url"])[:200],
                                  quote=passage))
    return items


def make_probe(
    executor, target: BreakTarget, language: str, package: str, *, run_id: str, public: bool
) -> Probe:
    """Run the failing step with one release of the package, starting from the checkpoint before it."""
    trace = target.trace
    assert trace is not None and trace.failed_unit, "make_probe needs the trace the step failed in"
    fu = trace.failed_unit
    prev = group_units(target.plan.steps)[fu - 1][-1]
    first = trace.results.get(target.step_id)
    original = key_line(failure_output(first)) if first else ""

    # stop at the broken step: later cells or steps failing for other reasons must not hide the answer
    upto = next(i for i, s in enumerate(target.plan.steps) if s.id == target.step_id)
    head = target.plan.model_copy(update={"steps": target.plan.steps[: upto + 1]})

    async def probe(version: str) -> str:
        spec = f"{package}=={version}" if language == "python" else f"{package}@{version}"
        cmd = (
            f"pip install -q --only-binary=:all: {shlex.quote(spec)}"
            if language == "python"
            else f"npm install --no-save --no-audit --no-fund {shlex.quote(spec)}"
        )
        plan = apply_edits(
            head,
            [
                Edit(
                    op="insert_after",
                    step_id=prev.id,
                    new_content=cmd,
                    new_kind="shell",
                    reason="Find the release that broke this step",
                )
            ],
        )
        t = await executor.execute(
            plan, trace.states, from_unit=fu, stop_after=fu + 1, run_id=run_id, public=public, quiet=True
        )
        if t.failed_unit == fu:
            return "no_install"
        r = t.results.get(target.step_id)
        if t.failed_unit is None or (r is not None and t.failing_step_id != target.step_id):
            return "pass"
        if t.failing_step_id != target.step_id or r is None:
            return "other"
        return "same" if _same_error(key_line(failure_output(r)), target.signature, original) else "other"

    return probe


async def find_break(target: BreakTarget, *, language: str, llm, registry, probe_for: Callable[[str], Probe],
                     max_probes: int, time_s: float, run_id: str, public: bool, search=None) -> BreakPoint | None:
    found: dict = {"notes": None}  # the release-notes answer so far, kept if the runs can't finish in time
    try:
        async with asyncio.timeout(time_s):
            return await _find(target, language, llm, registry, probe_for, max_probes, run_id, public, found,
                               search)
    except Exception as e:  # timeouts, budget stops, registry and sandbox errors: the run carries on without it
        log.info("break finder stopped: %r", e)
        return found["notes"]


async def _find(target, language, llm, registry, probe_for, max_probes, run_id, public, found,
                search) -> BreakPoint | None:
    installed = installed_versions(language, target.versions)
    guess = await _guess(llm, target, target.evidence, installed, run_id, public)
    named = ([guess.package] if guess else []) + [target.signature.package or ""]
    pkg = (next((norm(language, c) for c in named if norm(language, c) in installed), None)
           or _package_in_step(target.plan, target.step_id, target.signature, installed))
    if pkg is None:
        log.info("break finder: no installed package matches %r (installed: %d)", named, len(installed))
        return None
    current = _version(installed[pkg])
    rel = [r for r in await registry.releases(language, pkg) if current and (v := _version(r.version)) and v <= current]
    if not rel or Version(rel[-1].version) != current:
        log.info("break finder: no published release list ending at %s %s", pkg, current)
        return None  # the installed version isn't a published release; nothing to compare against

    def cited(g: BreakGuess | None, evidence: list[Evidence]) -> tuple[int, Evidence] | None:
        """A version the model read from a source: a real release, no newer than installed, stated by the quote
        or by the release note's own address (".../1.24.0-notes.html")."""
        if not g or not g.version or (g.package and norm(language, g.package) != pkg):
            return None
        gv = _version(g.version)
        i = next((i for i, r in enumerate(rel) if gv is not None and Version(r.version) == gv), None)
        ev = next((e for e in evidence if e.id == g.evidence_id), None)
        if i is None or ev is None:
            return None
        stated = (g.version, rel[i].version)
        return (i, ev) if _mentions(ev.quote, *stated) or _mentions(_address(ev.url), *stated) else None

    def from_notes(n: tuple[int, Evidence], lo: int | None) -> BreakPoint:
        r = rel[n[0]]
        good = rel[lo] if lo is not None and lo < n[0] else None
        return BreakPoint(package=pkg, step_id=target.step_id, first_bad=r.version, first_bad_date=r.date,
                          last_good=good.version if good else None, last_good_date=good.date if good else None,
                          how="release_notes", evidence_id=n[1].id, source=n[1])

    def ran(lo: int, hi: int) -> BreakPoint:
        ev = notes[1] if notes and notes[0] == hi else None
        return BreakPoint(package=pkg, step_id=target.step_id, first_bad=rel[hi].version, first_bad_date=rel[hi].date,
                          last_good=rel[lo].version, last_good_date=rel[lo].date, how="ran",
                          evidence_id=ev.id if ev else None, source=ev)

    def fits(n, lo, hi) -> bool:  # what we ran doesn't contradict the source
        return n is not None and (lo is None or lo < n[0]) and n[0] <= hi

    notes = cited(guess, target.evidence)
    lo, hi = None, len(rel) - 1  # lo: newest release that passes; hi: oldest release known to fail the same way
    found["notes"] = from_notes(notes, lo) if notes else None
    log.info("break finder: %s installed %s, cited version %s", pkg, current, rel[notes[0]].version if notes else None)
    raw_probe = probe_for(pkg)

    async def probe(version: str) -> str:
        try:
            return await raw_probe(version)
        except BudgetExceeded:
            raise  # stop: the run has no money left for more sandbox work
        except Exception as e:  # a sandbox error says nothing about the release
            log.info("break finder: probe %s %s failed: %r", pkg, version, e)
            return "other"

    tried: dict[int, str] = {}
    idx = notes[0] if notes else _series_start(rel, hi)
    while lo is None and len(tried) < max_probes:  # find a release that still runs the step, a series at a time
        pair = [i for i in (idx - 1, idx) if 0 <= i < hi and i not in tried]
        if not pair:
            break
        async with asyncio.TaskGroup() as tg:  # a budget stop in one probe cancels the other
            tasks = [tg.create_task(probe(rel[i].version)) for i in pair]
        tried.update(zip(pair, (t.result() for t in tasks)))
        log.info("break finder: %s", {rel[i].version: tried[i] for i in pair})
        old_hi = hi
        hi = min([hi] + [i for i in pair if tried[i] == "same"])
        passes = [i for i in pair if tried[i] == "pass"]
        if passes:
            lo = max(passes)
        found["notes"] = from_notes(notes, lo) if fits(notes, lo, hi) else None
        if lo is not None and lo > hi:
            return None  # a newer release passes where an older one fails the same way: no clean answer
        if lo is None and hi < old_hi and hi > 0:
            idx = _series_start(rel, hi)  # the change is older: step back to the series before
        elif lo is None:
            break  # older releases can't be run here
    while lo is not None and hi - lo > 1 and len(tried) < max_probes:  # narrow it down
        mid = (lo + hi) // 2
        tried[mid] = await probe(rel[mid].version)
        if tried[mid] == "pass":
            lo = mid
        elif tried[mid] == "same":
            hi = mid
        else:
            break
    if lo is not None and hi - lo == 1:
        return ran(lo, hi)
    if fits(notes, lo, hi):
        return from_notes(notes, lo)
    if notes is None and search is not None:  # running couldn't settle it: look for the release note itself
        extra = await _release_notes(search, pkg, target.signature, run_id, public)
        g2 = n2 = None
        for _ in range(2 if extra else 0):  # Lightning sometimes passes on a quote it reads correctly next time
            g2 = await _guess(llm, target, extra, installed, run_id, public)
            n2 = cited(g2, extra)
            if n2:
                break
        log.info("break finder: release notes %s -> %r, accepted %s", [e.url for e in extra], g2,
                 rel[n2[0]].version if n2 else None)
        if fits(n2, lo, hi):
            return from_notes(n2, lo)
    return None
