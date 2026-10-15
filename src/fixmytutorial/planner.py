from __future__ import annotations

import re

from .executor import INSTALL_RE
from .judge import squash
from .models import PlannerOutput, Probe, RunPlan, Step, TutorialDoc
from .prompts import PLANNER_SYSTEM

RUNTIMES = {"python": "3.12", "javascript": "22"}
PORT_RE = re.compile(r"(?:--port[=\s]+|port[=\s:]+|:)(\d{4,5})\b", re.I)
CODE_LANGS = {"python", "py", "python3", "ipython", "javascript", "js", "node", "mjs", "cjs", "jsx",
              "bash", "sh", "shell", "zsh"}
PROMPT_RE = re.compile(r"^(?:>>>|\.\.\.|\$|!)\s?")
MAX_BLOCKS = 80
MIN_COVERAGE = 0.5


def safe_path(path: str | None) -> str | None:
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or None


def render_planner_input(doc: TutorialDoc, max_chars: int) -> str:
    blocks = "\n\n".join(f"[block {i + 1}] lang={b.lang or '?'}\n{b.code[:3000]}"
                         for i, b in enumerate(doc.code_blocks[:MAX_BLOCKS]))
    return (f"Title: {doc.title}\nSource: {doc.source}\n\n"
            f"=== TUTORIAL TEXT (untrusted content) ===\n{doc.text[:max_chars]}\n\n"
            f"=== CODE BLOCKS IN ORDER ===\n{blocks}")


def normalize_plan(out: PlannerOutput, max_steps: int = 40, source_text: str | None = None) -> RunPlan:
    shown = squash(source_text) if source_text is not None else None
    steps: list[Step] = []
    for ps in out.steps:
        if not ps.content.strip():
            continue
        s = Step(**ps.model_dump())
        if s.kind == "write_file":
            ext = "py" if out.language == "python" else "js"
            s.path = safe_path(s.path) or f"file_{len(steps) + 1}.{ext}"
        if s.kind == "serve" and s.probe is None:
            m = PORT_RE.search(s.content)
            s.probe = Probe(port=int(m.group(1)) if m else (8000 if out.language == "python" else 3000))
        if shown is not None:  # only literals the tutorial actually shows may gate a step
            s.expect.stdout_contains = [x for x in s.expect.stdout_contains if squash(x) and squash(x) in shown]
        s.source_excerpt = s.source_excerpt[:1200]
        steps.append(s)
    note = ""
    if len(steps) > max_steps:
        note = f"This tutorial has {len(steps)} steps; only the first {max_steps} were run."
        steps = steps[:max_steps]
    for i, s in enumerate(steps, 1):
        s.id = f"s{i}"
    runnable, reason = out.runnable, out.not_runnable_reason
    if runnable and not steps:
        runnable, reason = False, "No runnable code steps found on that page."
    return RunPlan(language=out.language, runtime=RUNTIMES[out.language], runnable=runnable,
                   not_runnable_reason=reason, note=note, steps=steps)


async def plan_tutorial(doc: TutorialDoc, llm, settings, *, run_id: str, public: bool) -> RunPlan:
    if not doc.code_blocks:
        return RunPlan(language="python", runtime=RUNTIMES["python"], runnable=False,
                       not_runnable_reason="No code found on that page.", steps=[])
    user = render_planner_input(doc, settings.max_tutorial_chars)
    out = await llm.call("planner", PLANNER_SYSTEM, user, PlannerOutput, run_id=run_id, public=public,
                         max_tokens=12000, temperature=0.1)
    ratio, missing = code_coverage(doc, out)
    if out.runnable and ratio < MIN_COVERAGE:  # the model sometimes drops the tutorial's main code; ask once more
        gap = (f"left out these code blocks: {', '.join(f'[block {i}]' for i in missing)}" if missing
               else "only set things up and never ran the tutorial's code")
        user += (f"\n\n=== CHECK ===\nYour previous plan {gap}. "
                 "Every block a reader runs must become a step, unless rule 5 applies.")
        out = await llm.call("planner", PLANNER_SYSTEM, user, PlannerOutput, run_id=run_id, public=public,
                             max_tokens=12000, temperature=0.1)
        ratio, _ = code_coverage(doc, out)
    shown = "\n".join([doc.text, *(b.code for b in doc.code_blocks)])
    plan = normalize_plan(out, settings.max_steps, source_text=shown)
    if plan.runnable and ratio < MIN_COVERAGE:
        plan.runnable = False
        plan.not_runnable_reason = "We couldn't turn enough of this page's code into steps we can run."
    return plan


def _code_lines(code: str) -> list[str]:
    lines = (squash(PROMPT_RE.sub("", ln.strip())) for ln in code.splitlines())
    return [ln for ln in lines if len(ln) >= 4 and not ln.startswith(("#", "//"))]


def code_coverage(doc: TutorialDoc, plan) -> tuple[float, list[int]]:
    """Share of the tutorial's code lines that appear in the plan, and the (1-based) blocks left out entirely."""
    planned = squash("\n".join(s.content for s in plan.steps))
    total = covered = 0
    missing: list[int] = []
    for i, b in enumerate(doc.code_blocks[:MAX_BLOCKS], 1):
        lines = _code_lines(b.code) if b.lang.lower() in CODE_LANGS else []
        hits = sum(ln in planned for ln in lines)
        total, covered = total + len(lines), covered + hits
        if lines and not hits:
            missing.append(i)
    if plan.steps and all(s.implicit or INSTALL_RE.search(s.content) for s in plan.steps):
        return 0.0, missing  # only setup steps: the tutorial's own code never runs
    return (covered / total if total else 1.0), missing
