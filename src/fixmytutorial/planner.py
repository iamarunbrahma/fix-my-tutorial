from __future__ import annotations

import re
from difflib import SequenceMatcher

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


DATA_EXT = (".csv", ".tsv", ".xlsx", ".xls", ".parquet", ".dat", ".data", ".json", ".txt", ".xml")
CONFIG_FILES = {"package.json", "package-lock.json", "tsconfig.json", "jsconfig.json", "requirements.txt", ".babelrc",
                ".eslintrc.json", "composer.json"}
URL_RE = re.compile(r"https?://[^\s'\"<>)]+")


def is_data_file(path: str | None) -> bool:
    name = (path or "").rsplit("/", 1)[-1].lower()
    return name.endswith(DATA_EXT) and name not in CONFIG_FILES


def invents_data(step: Step, shown: str) -> bool:
    """A step the planner added that makes up data the page never shows, or downloads from a link it never gives."""
    if step.kind == "write_file" and is_data_file(step.path) and squash(step.content) not in shown:
        return True
    if INSTALL_RE.search(step.content):
        return False  # an install may name a package index (pip --index-url ...)
    return any(u.rstrip(".,;") not in shown for u in URL_RE.findall(step.content))


def default_probe(content: str, language: str) -> Probe:
    """The port a server command listens on: an explicit port, else the framework's default."""
    m = PORT_RE.search(content)
    port = int(m.group(1)) if m else next((p for rx, p in FRAMEWORK_PORTS if rx.search(content)),
                                          8000 if language == "python" else 3000)
    return Probe(port=port, expect_status=0)  # no URL from the tutorial: any answer means it is up


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
        if s.kind == "shell" and (m := IPYTHON_HEREDOC.match(s.content)):
            s.kind, s.content = "run_code", m.group(2)  # ipython exits 0 even when the code raises
        if s.kind == "run_code" and out.language == "python" and (m := IMPORT_ONE.match(s.content)):
            if any(w.kind == "write_file" and w.path == f"{m.group(1)}.py" for w in steps):
                # importing a script skips its `if __name__ == "__main__":` block; the tutorial runs it as a script
                s.kind, s.content = "shell", f"python {m.group(1)}.py"
        if s.kind == "shell" and (m := IPYTHON_FILE.match(s.content)):
            written = [w for w in steps if w.kind == "write_file" and w.path == safe_path(m.group(1))]
            if written:  # ipython runs a .py file as plain Python, so its notebook magics would be syntax errors
                s.kind, s.content = "run_code", written[-1].content
        if s.kind == "write_file":
            ext = "py" if out.language == "python" else "js"
            s.path = safe_path(s.path) or f"file_{len(steps) + 1}.{ext}"
            # writing a file prints nothing of the program; the step that runs it carries the expected output
            s.expect.stdout_contains, s.expect.description = [], None
        if s.kind == "serve" and s.probe is None:
            s.probe = default_probe(s.content, out.language)
        if shown is not None:
            s.content = _unflatten(s.content, shown)
        if shown is not None and s.kind == "shell" and INSTALL_RE.search(s.content):
            s.content = unpin(s.content, shown)
        if shown is not None and s.implicit and invents_data(s, shown):
            continue  # a stand-in file or a guessed URL would make a fake break, and then a fake fix
        if shown is not None:  # only literals the tutorial actually shows may gate a step
            s.expect.stdout_contains = [x for x in s.expect.stdout_contains if squash(x) and squash(x) in shown]
        # A step is expected to do what the tutorial intends. A planner that predicts the breakage ("exit 1, it will
        # fail because...") would make every working fix look wrong.
        s.expect.exit_code = 0
        s.expect.stdout_contains = [x for x in s.expect.stdout_contains if not predicts_failure(x)]
        if s.expect.description and (predicts_failure(s.expect.description)
                                     or VISUAL.search(s.expect.description)):  # pictures can't be checked from text
            s.expect.description = None
        s.source_excerpt = s.source_excerpt[:1200]
        steps.append(s)
    if out.language == "python" and any(s.kind != "shell" and IPYTHON_IMPORT.search(s.content) for s in steps) and not any(
            s.kind == "shell" and INSTALL_RE.search(s.content) and re.search(r"\bipython\b", s.content, re.I) for s in steps):
        # anyone reading a notebook has IPython; without it `from IPython.display import ...` fails on our setup
        pip = next((s for s in steps if s.kind == "shell" and re.search(r"\bpip3? install\b", s.content)), None)
        if pip:
            pip.content = pip.content.rstrip() + " ipython"
        else:
            steps.insert(0, Step(id="s0", title="Install IPython", kind="shell", content="pip install ipython",
                                 implicit=True))
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
    out = restore_steps(out, doc)
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
        again = restore_steps(again, doc)
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
PREDICTED_FAILURE = re.compile(r"\b(fail|fails|failing|error|errors|exception|crash|raises?|traceback|removed|deprecated|"
                               r"no longer)\b|\bno (attribute|module)\b", re.I)
EXCEPTION_NAME = re.compile(r"[a-z](Error|Exception)\b")  # AttributeError, ModuleNotFoundError; case matters here


def predicts_failure(text: str) -> bool:
    """An expectation that names the breakage ("AttributeError: ... has no attribute 'append'") isn't what the
    tutorial intends, and would reject every working fix."""
    return bool(PREDICTED_FAILURE.search(text) or EXCEPTION_NAME.search(text))
VISUAL = re.compile(r"\b(plot|plots|plotted|chart|graph|figure|histogram|scatter|heatmap|image|visuali[sz]ation|"
                    r"legend|axes|line plot|bar plot|pie)\b", re.I)
PLOT_LIBS = re.compile(r"\b(matplotlib|seaborn|plotly|pyplot|plt\.)")
PLOT_REASON = re.compile(r"show\(|plot|chart|figure|display|gui|headless|window|graphic", re.I)


def _skipped_for_plots(doc: TutorialDoc, reason: str | None) -> bool:
    return bool(reason and PLOT_REASON.search(reason) and any(PLOT_LIBS.search(b.code) for b in doc.code_blocks))


CODE_SIGN = re.compile(r"^\s*(?:>>>\s*)?(?:import \w|from [\w.]+ import |def \w|class \w|print\s*\(|print \"|for \w+ in |"
                       r"while |with |try:|function\b|const \w|let \w|var \w|require\(|console\.|module\.exports|"
                       r"(?:np|pd|plt|sns|tf|torch)\.\w)", re.M)


def is_code_block(b) -> bool:
    """A block a reader runs: tagged as code, or untagged (older blogs rarely tag) with an unmistakable code line.
    Printed output has none, so it never counts."""
    lang = (b.lang or "").lower()
    if lang in CODE_LANGS:
        return True
    return lang in ("", "text", "none", "code", "plaintext") and bool(CODE_SIGN.search(b.code))


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
        if is_code_block(b):
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


TOKEN_RE = re.compile(r"\w+")
QUOTED_PIN = re.compile(r"""(['"])([A-Za-z0-9][\w.\-\[\]]*)\s*((?:==|>=|<=|~=|!=|<|>)[^'"\s]*)\1""")
BARE_PIN = re.compile(r"(?<![\w@/'\"-])([A-Za-z0-9][\w.\-\[\]]*)((?:==|~=|!=)[\w.*+!\-]+)")
NPM_PIN = re.compile(r"(?<![\w@/.-])((?:@[\w.\-]+/)?[\w.\-]+)@([\w.^~*\-]+)")


def unpin(content: str, shown: str) -> str:
    """Drop a version pin the tutorial never gives: running today's versions is how a break is found, and an old pin
    can fail to install on today's Python, which would be our break, not the tutorial's."""
    page = re.sub(r"\s", "", shown)

    def on_page(spec: str) -> bool:
        return re.sub(r"\s", "", spec) in page

    content = QUOTED_PIN.sub(lambda m: m.group(0) if on_page(m.group(2) + m.group(3)) else m.group(2), content)
    content = BARE_PIN.sub(lambda m: m.group(0) if on_page(m.group(0)) else m.group(1), content)
    if re.search(r"\b(npm|yarn|pnpm)\b", content):
        content = NPM_PIN.sub(lambda m: m.group(0) if on_page(m.group(0)) else m.group(1), content)
    return content


IPYTHON_IMPORT = re.compile(r"^\s*(?:from|import)\s+IPython\b", re.M)
IMPORT_ONE = re.compile(r"^\s*import\s+(\w+)\s*$")
IPYTHON_FILE = re.compile(r"^\s*ipython3?\s+(\S+\.py)\s*$")
IPYTHON_HEREDOC = re.compile(r"^\s*ipython3?\b[^\n<]*<<-?\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\1\s*$", re.S)


def restore_page_code(content: str, blocks: list[str]) -> str:
    """Put back page code the planner changed (`x.dim` for the page's `x.ndim`): a line not on the page becomes the
    line of the step's own block with the same shape and one word different, if that word is close enough. Lines
    the planner added (an extra import, a call the page never makes) have no such twin and stay as they are."""
    page = squash("\n".join(blocks))
    lines = content.split("\n")

    def on_page(ln: str) -> bool:  # whole words only: `dim, n = 2` is not on a page that says `ndim, n = 2`
        return re.search(rf"(?<!\w){re.escape(squash(ln))}(?!\w)", page) is not None

    off = [i for i, ln in enumerate(lines) if ln.strip() and not on_page(ln)]
    present = {squash(ln) for ln in lines if ln.strip()}
    if not off or not blocks:
        return content
    best = max(blocks, key=lambda b: sum(1 for p in present if p in squash(b)))
    if not any(p in squash(best) for p in present):
        return content  # the step shares nothing with the page: nothing to restore from
    twins = [ln.strip() for ln in best.split("\n") if ln.strip()]
    for i in off:
        line = lines[i].strip()
        shape, words = TOKEN_RE.sub("x", line), TOKEN_RE.findall(line)
        choice, score = None, 0.5
        for t in twins:
            if squash(t) in present or TOKEN_RE.sub("x", t) != shape:
                continue
            diff = [(a, b) for a, b in zip(words, TOKEN_RE.findall(t)) if a != b]
            if len(diff) == 1 and (r := SequenceMatcher(None, *diff[0]).ratio()) >= score:
                choice, score = t, r
        if choice:
            lines[i] = lines[i][: len(lines[i]) - len(lines[i].lstrip())] + choice
            present.add(squash(choice))
    return "\n".join(lines)


def restore_steps(out: PlannerOutput, doc: TutorialDoc) -> PlannerOutput:
    blocks = [b.code for b in doc.code_blocks[:MAX_BLOCKS] if is_code_block(b)]
    for st in out.steps:
        if not st.implicit and st.kind in ("run_code", "write_file"):
            st.content = restore_page_code(st.content, blocks)
    return out


def code_coverage(doc: TutorialDoc, plan) -> tuple[float, list[int]]:
    """Share of the tutorial's code lines that the plan runs, and the (1-based) blocks left out entirely. Code in a
    file that nothing ever runs does not count: writing it proves nothing."""
    unrun = set(unrun_files(plan))
    planned = squash("\n".join(s.content for s in plan.steps if not (s.kind == "write_file" and s.path in unrun)))
    total = covered = 0
    missing: list[int] = []
    for i, b in enumerate(doc.code_blocks[:MAX_BLOCKS], 1):
        lines = _code_lines(b.code) if is_code_block(b) else []
        hits = sum(ln in planned for ln in lines)
        total, covered = total + len(lines), covered + hits
        if lines and not hits:
            missing.append(i)
    if plan.steps and all(s.implicit or INSTALL_RE.search(s.content) for s in plan.steps):
        return 0.0, missing  # only setup steps: the tutorial's own code never runs
    return (covered / total if total else 1.0), missing
