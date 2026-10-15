from __future__ import annotations

import re

from .ledger import BudgetExceeded
from .models import FailureSignature, Step, StepResult
from .prompts import TRIAGE_SYSTEM


def _pkg(name: str | None) -> str | None:
    return name.split(".")[0] if name else None


HEURISTICS = [
    (re.compile(r"timed out after (\d+)s"), "timeout", "Timeout", lambda m: {}),
    (re.compile(r"APIRemovedInV1"), "removed_api", "APIRemovedInV1",
     lambda m: {"package": "openai", "symbol": "openai.ChatCompletion"}),
    (re.compile(r"`?(\w+)`? has been removed from ([\w\-\.]+)"), "removed_api", "Removed",
     lambda m: {"symbol": m.group(1), "package": m.group(2).rstrip(".")}),
    (re.compile(r"cannot import name '(\w+)' from '([\w\.]+)'"), "removed_api", "ImportError",
     lambda m: {"symbol": m.group(1), "package": _pkg(m.group(2))}),
    (re.compile(r"No module named '([\w\.]+)'"), "renamed_package", "ModuleNotFoundError",
     lambda m: {"package": _pkg(m.group(1)), "symbol": m.group(1)}),
    (re.compile(r"module '([\w\.]+)' has no attribute '(\w+)'"), "removed_api", "AttributeError",
     lambda m: {"package": _pkg(m.group(1)), "symbol": f"{m.group(1)}.{m.group(2)}"}),
    (re.compile(r"'(\w+)' object has no attribute '(\w+)'"), "removed_api", "AttributeError",
     lambda m: {"symbol": f"{m.group(1)}.{m.group(2)}"}),
    (re.compile(r"got an unexpected keyword argument '(\w+)'"), "removed_api", "TypeError",
     lambda m: {"symbol": m.group(1)}),
    (re.compile(r"ERR_REQUIRE_ESM|require\(\) of ES Module"), "version_conflict", "ERR_REQUIRE_ESM", lambda m: {}),
    (re.compile(r"ERESOLVE|ResolutionImpossible|Could not find a version that satisfies"), "version_conflict",
     "DependencyResolution", lambda m: {}),
    (re.compile(r"No matching distribution found for ([\w\-\.\[\]]+)"), "renamed_package", "PipNoDistribution",
     lambda m: {"package": m.group(1)}),
    (re.compile(r"HTTP Error 404|404 Client Error|404 Not Found"), "dead_resource", "HTTP404", lambda m: {}),
    (re.compile(r"(\S+): command not found|command not found: (\S+)"), "changed_cli", "CommandNotFound",
     lambda m: {"symbol": m.group(1) or m.group(2)}),
    (re.compile(r"is not a function"), "removed_api", "TypeError", lambda m: {}),
    (re.compile(r"SyntaxError"), "tutorial_typo", "SyntaxError", lambda m: {}),
]
KEY_LINE_RE = re.compile(
    r"^\s*(?:[\w\.]*(?:Error|Exception|APIRemovedInV1)\b.*|npm ERR!.*|Error:.*|ERROR:.*|\[Fix My Tutorial\].*)$", re.M)


def key_line(output: str) -> str:
    text = output or ""
    matches = list(KEY_LINE_RE.finditer(text))
    if matches:
        m = matches[-1]
        line = m.group(0).strip()
        if line.endswith(":"):  # e.g. "ImportError:" with the message on the following lines
            rest = next((ln.strip() for ln in text[m.end():].splitlines() if ln.strip()), "")
            line = f"{line} {rest}".strip()
        return line[:300]
    lines = [ln for ln in (output or "").strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:300]


def failure_output(result: StepResult) -> str:
    return (result.stdout[-4000:] + "\n" + result.stderr[-6000:]).strip()


def _queries(kl: str, package: str | None, symbol: str | None, language: str) -> list[str]:
    qs = [kl[:200]] if kl else []
    if package and symbol:
        qs.append(f"{package} {symbol} removed deprecated replacement")
    elif symbol:
        qs.append(f"{language} {symbol} removed deprecated replacement")
    elif package:
        qs.append(f"{package} changelog breaking changes")
    return qs[:3] or [f"{language} error {kl}"[:200]]


def heuristic_signature(output: str, language: str) -> FailureSignature:
    kl = key_line(output)
    for rx, category, error_type, fields in HEURISTICS:
        m = rx.search(output)
        if m:
            f = fields(m)
            return FailureSignature(category=category, error_type=error_type, key_line=kl, **f,
                                    search_queries=_queries(kl, f.get("package"), f.get("symbol"), language))
    return FailureSignature(category="unknown", error_type=kl.split(":")[0][:60], key_line=kl,
                            search_queries=_queries(kl, None, None, language))


async def triage(step: Step, result: StepResult, *, language: str, llm, run_id: str,
                 public: bool) -> FailureSignature:
    output = failure_output(result)
    base = heuristic_signature(output, language)
    if base.category == "timeout" or llm is None:
        return base
    user = (f"Language: {language}\nFailing step ({step.kind}):\n{step.content[:2500]}\n\n"
            f"Heuristic guess: {base.model_dump_json()}\n\nOutput (last 150 lines):\n"
            + "\n".join(output.splitlines()[-150:]))
    try:
        sig = await llm.call("triage", TRIAGE_SYSTEM, user, FailureSignature, run_id=run_id, public=public,
                             max_tokens=800, temperature=0)
    except BudgetExceeded:
        raise
    except Exception:
        return base
    if not sig.key_line or sig.key_line.rstrip().endswith(":"):
        sig.key_line = base.key_line
    if sig.category == "unknown":  # the model added nothing; keep what the error text itself says
        sig.category = base.category
    sig.package, sig.symbol = sig.package or base.package, sig.symbol or base.symbol
    sig.search_queries = [q[:200] for q in (sig.search_queries or base.search_queries)[:3]]
    return sig
