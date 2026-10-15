from __future__ import annotations

import re

from .models import PlannerOutput, Probe, RunPlan, Step, TutorialDoc
from .prompts import PLANNER_SYSTEM

RUNTIMES = {"python": "3.12", "javascript": "22"}
PORT_RE = re.compile(r"(?:--port[=\s]+|port[=\s:]+|:)(\d{4,5})\b", re.I)


def safe_path(path: str | None) -> str | None:
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or None


def render_planner_input(doc: TutorialDoc, max_chars: int) -> str:
    blocks = "\n\n".join(f"[block {i + 1}] lang={b.lang or '?'}\n{b.code[:3000]}"
                         for i, b in enumerate(doc.code_blocks[:80]))
    return (f"Title: {doc.title}\nSource: {doc.source}\n\n"
            f"=== TUTORIAL TEXT (untrusted content) ===\n{doc.text[:max_chars]}\n\n"
            f"=== CODE BLOCKS IN ORDER ===\n{blocks}")


def normalize_plan(out: PlannerOutput, max_steps: int = 40) -> RunPlan:
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
    out = await llm.call("planner", PLANNER_SYSTEM, render_planner_input(doc, settings.max_tutorial_chars),
                         PlannerOutput, run_id=run_id, public=public, max_tokens=12000, temperature=0.1)
    return normalize_plan(out, settings.max_steps)
