from __future__ import annotations

import re

from .executor import INSTALL_RE
from .judge import squash
from .llm import LLMOutputError
from .models import PlannerOutput, Probe, RunPlan, Step, TutorialDoc
from .prompts import PLANNER_SYSTEM

RUNTIMES = {"python": "3.12", "javascript": "22"}
PORT_RE = re.compile(r"(?:--port[=\s]+|port[=\s:]+|:)(\d{4,5})\b", re.I)
FRAMEWORK_PORTS = [(re.compile(r"\bflask\b", re.I), 5000), (re.compile(r"\bstreamlit\b", re.I), 8501),
                   (re.compile(r"\bgradio\b", re.I), 7860), (re.compile(r"\bvite\b", re.I), 5173),
                   (re.compile(r"\b(uvicorn|fastapi|runserver|http\.server)\b", re.I), 8000)]
CODE_LANGS = {"python", "py", "python3", "ipython", "javascript", "js", "node", "mjs", "cjs", "jsx",
              "bash", "sh", "shell", "zsh"}
PROMPT_RE = re.compile(r"^(?:>>>|\.\.\.|\$|!)\s?")
MAX_BLOCKS = 80
CODE_EXTS = (".py", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx")
MIN_COVERAGE = 0.5


def safe_path(path: str | None) -> str | None:
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or None


def default_probe(content: str, language: str) -> Probe:
    """The port a server command listens on: an explicit port, else the framework's default."""
    m = PORT_RE.search(content)
    if m:
        return Probe(port=int(m.group(1)))
    port = next((p for rx, p in FRAMEWORK_PORTS if rx.search(content)), 8000 if language == "python" else 3000)
    return Probe(port=port)


def render_planner_input(doc: TutorialDoc, max_chars: int) -> str:
    blocks = "\n\n".join(f"[block {i + 1}] lang={b.lang or '?'}\n{b.code[:3000]}"
                         for i, b in enumerate(doc.code_blocks[:MAX_BLOCKS]))
    return (f"Title: {doc.title}\nSource: {doc.source}\n\n"
            f"=== TUTORIAL TEXT (untrusted content) ===\n{doc.text[:max_chars]}\n\n"
            f"=== CODE BLOCKS IN ORDER ===\n{blocks}")


def _unflatten(content: str, shown: str) -> str:
    """Undo code the model returned as one line of literal \\n escapes (it would fail as written and then be
    "fixed" back to the page's own code). Only when the page itself has those lines and not the escaped one."""
    if "\n" in content.strip() or "\\n" not in content:
        return content
    fixed = content.replace("\\n", "\n").replace("\\t", "\t")
    lines = [squash(ln) for ln in fixed.splitlines() if ln.strip()]
    if len(lines) > 1 and squash(content) not in shown and all(ln in shown for ln in lines):
        return fixed
    return content


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
            s.probe = default_probe(s.content, out.language)
        if shown is not None:
            s.content = _unflatten(s.content, shown)
        if shown is not None:  # only literals the tutorial actually shows may gate a step
            s.expect.stdout_contains = [x for x in s.expect.stdout_contains if squash(x) and squash(x) in shown]
        # A step is expected to do what the tutorial intends. A planner that predicts the breakage ("exit 1, it will
        # fail because...") would make every working fix look wrong.
        s.expect.exit_code = 0
        if s.expect.description and (PREDICTED_FAILURE.search(s.expect.description)
                                     or VISUAL.search(s.expect.description)):  # pictures can't be checked from text
            s.expect.description = None
        s.source_excerpt = s.source_excerpt[:1200]
        steps.append(s)
    note, truncated = "", len(steps) > max_steps
    if truncated:
        note = f"This tutorial has {len(steps)} steps; only the first {max_steps} were run."
        steps = steps[:max_steps]
    for i, s in enumerate(steps, 1):
        s.id = f"s{i}"
    runnable, reason = out.runnable, out.not_runnable_reason
    if runnable and not steps:
        runnable, reason = False, "No runnable code steps found on that page."
    return RunPlan(language=out.language, runtime=RUNTIMES[out.language], runnable=runnable,
                   not_runnable_reason=reason, note=note, steps=steps, truncated=truncated)


async def plan_tutorial(doc: TutorialDoc, llm, settings, *, run_id: str, public: bool) -> RunPlan:
    if not doc.code_blocks:
        return RunPlan(language="python", runtime=RUNTIMES["python"], runnable=False,
                       not_runnable_reason="No code found on that page.", steps=[])
    user = render_planner_input(doc, settings.max_tutorial_chars)
    try:
        out = await llm.call("planner", PLANNER_SYSTEM, user, PlannerOutput, run_id=run_id, public=public,
                             max_tokens=12000, temperature=0.1)
    except LLMOutputError:  # now and then the answer runs on until it is cut off: one fresh, compact try
        out = await llm.call("planner", PLANNER_SYSTEM, f"{user}\n\n{COMPACT}", PlannerOutput, run_id=run_id,
                             public=public, max_tokens=12000, temperature=0.0)
    if not out.runnable and _skipped_for_plots(doc, out.not_runnable_reason):  # charts run headless: ask again
        retry = (f"{user}\n\n=== CHECK ===\nYou set runnable=false because of plots ({out.not_runnable_reason}). "
                 "The sandbox is headless with a non-interactive backend: plt.show() simply returns and figures "
                 "render fine. Plan the tutorial as runnable unless something else in rule 5 applies.")
        again = await llm.call("planner", PLANNER_SYSTEM, retry, PlannerOutput, run_id=run_id, public=public,
                               max_tokens=12000, temperature=0.1)
        if again.runnable:
            out = again
    ratio, missing = code_coverage(doc, out)
    if out.runnable and ratio < 1.0:  # dropped or quietly "modernized" code would hide the rot: ask once more
        unrun = unrun_files(out)
        parts = [f"left out these code blocks: {', '.join(f'[block {i}]' for i in missing)}" if missing
                 else "only set things up and never ran the tutorial's code" if ratio == 0 and not unrun else ""]
        if unrun:
            parts.append(f"writes {', '.join(unrun)} but never runs them (add the command the tutorial uses, even "
                         "when it is only mentioned in the text)")
        gap = "; ".join(x for x in parts if x) or "changed or left out some of the tutorial's code"
        lines = "\n".join(f"  {ln}" for ln in missing_lines(doc, out))
        retry = (f"{user}\n\n=== CHECK ===\nYour previous plan {gap}. Every block a reader runs must become a step, "
                 "unless rule 5 applies, with its code exactly as written."
                 + (f" These lines are missing or changed:\n{lines}" if lines else ""))
        again = await llm.call("planner", PLANNER_SYSTEM, retry, PlannerOutput, run_id=run_id, public=public,
                               max_tokens=12000, temperature=0.1)
        ratio_again, _ = code_coverage(doc, again)
        if ratio_again >= ratio:
            out, ratio = again, ratio_again
    shown = "\n".join([doc.text, *(b.code for b in doc.code_blocks)])
    plan = normalize_plan(out, settings.max_steps, source_text=shown)
    plan.coverage, plan.truncated = round(ratio, 3), plan.truncated or doc.truncated
    if plan.runnable and ratio < MIN_COVERAGE:
        plan.runnable = False
        plan.not_runnable_reason = "We couldn't turn enough of this page's code into steps we can run."
    return plan


COMPACT = ("Reply with compact JSON only: no reasoning text, keep each source_excerpt under 200 characters, and do "
           "not repeat yourself.")
PREDICTED_FAILURE = re.compile(r"\b(fail|fails|failing|error|exception|crash|raises?|traceback)\b", re.I)
VISUAL = re.compile(r"\b(plot|plots|plotted|chart|graph|figure|histogram|scatter|heatmap|image|visuali[sz]ation|"
                    r"legend|axes|line plot|bar plot|pie)\b", re.I)
PLOT_LIBS = re.compile(r"\b(matplotlib|seaborn|plotly|pyplot|plt\.)")
PLOT_REASON = re.compile(r"show\(|plot|chart|figure|display|gui|headless|window|graphic", re.I)


def _skipped_for_plots(doc: TutorialDoc, reason: str | None) -> bool:
    return bool(reason and PLOT_REASON.search(reason) and any(PLOT_LIBS.search(b.code) for b in doc.code_blocks))


def _code_lines(code: str) -> list[str]:
    raw = [ln.strip() for ln in code.splitlines()]
    if any(ln.startswith(">>>") for ln in raw):  # an interactive session: lines without a prompt are output
        raw = [ln for ln in raw if ln.startswith((">>>", "..."))]
    lines = (squash(PROMPT_RE.sub("", ln)) for ln in raw)
    return [ln for ln in lines if len(ln) >= 4 and not ln.startswith(("#", "//"))]


def missing_lines(doc: TutorialDoc, plan, limit: int = 12) -> list[str]:
    planned = squash("\n".join(s.content for s in plan.steps))
    out: list[str] = []
    for b in doc.code_blocks[:MAX_BLOCKS]:
        if b.lang.lower() in CODE_LANGS:
            out += [ln for ln in _code_lines(b.code) if ln not in planned and ln not in out]
    return out[:limit]


def unrun_files(plan) -> list[str]:
    """Code files the plan writes that no other step runs or imports (by file name or module name)."""
    out = []
    for i, s in enumerate(plan.steps):
        if s.kind != "write_file" or not s.path or not s.path.lower().endswith(CODE_EXTS):
            continue
        base = s.path.replace("\\", "/").split("/")[-1]
        stem = base.rsplit(".", 1)[0]
        others = "\n".join(o.content for j, o in enumerate(plan.steps) if j != i)
        if base not in others and not (len(stem) >= 3 and re.search(rf"\b{re.escape(stem)}\b", others)):
            out.append(s.path)
    return out


def code_coverage(doc: TutorialDoc, plan) -> tuple[float, list[int]]:
    """Share of the tutorial's code lines that the plan runs, and the (1-based) blocks left out entirely. Code in a
    file that nothing ever runs does not count: writing it proves nothing."""
    unrun = set(unrun_files(plan))
    planned = squash("\n".join(s.content for s in plan.steps if not (s.kind == "write_file" and s.path in unrun)))
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
