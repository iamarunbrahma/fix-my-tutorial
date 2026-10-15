"""Everything people share from a run: code changes with reasons and sources, a badge, and a preview image."""
from __future__ import annotations

import difflib
import io
import textwrap
from dataclasses import dataclass, field
from html import escape

from PIL import Image, ImageDraw, ImageFont

from .models import Evidence, RunRecord, Verdict

VERDICT_TEXT = {
    Verdict.STILL_WORKS: "Still works",
    Verdict.FIXED: "Fixed",
    Verdict.COULD_NOT_FIX: "Could not fix",
    Verdict.NOT_RUNNABLE: "Not runnable",
    Verdict.ERROR: "Check failed",
}
VERDICT_COLOR = {
    Verdict.STILL_WORKS: "#2e7d32", Verdict.FIXED: "#1565c0", Verdict.COULD_NOT_FIX: "#b26a00",
    Verdict.NOT_RUNNABLE: "#616161", Verdict.ERROR: "#9e2a2b",
}


@dataclass
class Change:
    step_id: str
    title: str
    kind: str
    before: str
    after: str
    diff: str
    reason: str
    citations: list[Evidence] = field(default_factory=list)
    strategy: str = "migrate"
    added: bool = False


def build_changes(rec: RunRecord) -> list[Change]:
    if not rec.plan or not rec.final_plan or rec.verdict == Verdict.STILL_WORKS:
        return []
    original = {s.id: s for s in rec.plan.steps}
    evidence = {e.id: e for r in rec.repairs for e in r.evidence}
    strategies: dict[str, str] = {}
    for r in rec.repairs:
        if r.winner:
            for e in r.winner.edits:
                strategies.setdefault(e.step_id if e.op == "replace" else f"{e.step_id}_fix", r.winner.strategy)
    changes = []
    for s in rec.final_plan.steps:
        o = original.get(s.id)
        if o is not None and o.content == s.content:
            continue
        before = o.content if o else ""
        diff = "\n".join(difflib.unified_diff(before.splitlines(), s.content.splitlines(), fromfile="tutorial",
                                              tofile="fixed", lineterm="", n=2))
        strategy = strategies.get(s.id) or next((v for k, v in strategies.items() if s.id.startswith(k)), "migrate")
        changes.append(Change(step_id=s.id, title=s.title, kind=s.kind, before=before, after=s.content, diff=diff,
                              reason=s.fix_reason, citations=[evidence[i] for i in s.fix_evidence_ids if i in evidence],
                              strategy=strategy, added=o is None))
    return changes


def diff_lines(change: Change) -> list[tuple[str, str]]:
    if change.added:
        return [("add", "+" + line) for line in change.after.splitlines()]
    out = []
    for line in change.diff.splitlines():
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            out.append(("hunk", line))
        elif line.startswith("+"):
            out.append(("add", line))
        elif line.startswith("-"):
            out.append(("del", line))
        else:
            out.append(("ctx", line))
    return out


def _runtime(rec: RunRecord) -> str:
    plan = rec.final_plan or rec.plan
    if not plan:
        return ""
    return ("Python " if plan.language == "python" else "Node ") + plan.runtime


def _partial(rec: RunRecord) -> bool:
    return bool(rec.plan) and (rec.plan.coverage < 1.0 or rec.plan.truncated)


def partial_note(rec: RunRecord) -> str:
    """Said wherever a verdict is shown, so a partial check is never presented as a full one."""
    if not _partial(rec):
        return ""
    if rec.plan.truncated:
        return "Partly checked: only the first part of this page was run."
    return f"Partly checked: we ran {int(rec.plan.coverage * 100)}% of the code on this page."


def badge_label(rec: RunRecord) -> str:
    month = (rec.finished_at or rec.created_at).strftime("%b %Y")
    if rec.verdict == Verdict.STILL_WORKS:
        return f"{'partly checked' if _partial(rec) else 'verified working'} · {_runtime(rec)} · {month}"
    if rec.verdict == Verdict.FIXED:
        return f"fixed version available{' · partly checked' if _partial(rec) else ''} · {month}"
    if rec.verdict == Verdict.COULD_NOT_FIX:
        return f"broken · {month}"
    if rec.verdict == Verdict.NOT_RUNNABLE:
        return "not runnable"
    return "check failed"


def badge_svg(rec: RunRecord) -> str:
    left, right = "Fix My Tutorial", badge_label(rec)
    lw, rw = 7 * len(left) + 16, 7 * len(right) + 16
    color = VERDICT_COLOR.get(rec.verdict, "#616161")
    label = escape(f"{left}: {right}")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{lw + rw}" height="20" role="img" aria-label="{label}">'
            f'<title>{label}</title><rect width="{lw}" height="20" fill="#24292f"/>'
            f'<rect x="{lw}" width="{rw}" height="20" fill="{color}"/>'
            f'<g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">'
            f'<text x="{lw / 2}" y="14" text-anchor="middle">{escape(left)}</text>'
            f'<text x="{lw + rw / 2}" y="14" text-anchor="middle">{escape(right)}</text></g></svg>')


def og_png(rec: RunRecord) -> bytes:
    img = Image.new("RGB", (1200, 630), "#0f1115")
    d = ImageDraw.Draw(img)
    big, mid, small = (ImageFont.load_default(size=s) for s in (60, 40, 30))
    d.text((70, 60), "Fix My Tutorial", font=small, fill="#9aa4b2")
    title = textwrap.shorten(rec.title or rec.source, 90, placeholder="...")
    for i, line in enumerate(textwrap.wrap(title, 34)[:3]):
        d.text((70, 130 + i * 74), line, font=big, fill="#f5f7fa")
    label = VERDICT_TEXT.get(rec.verdict, "Checked")
    width = d.textlength(label, font=mid) + 48
    d.rounded_rectangle((70, 420, 70 + width, 492), radius=16, fill=VERDICT_COLOR.get(rec.verdict, "#616161"))
    d.text((94, 434), label, font=mid, fill="#ffffff")
    n = len(build_changes(rec))
    sub = f"{n} change{'s' if n != 1 else ''}, each with a cited reason" if n else _runtime(rec)
    d.text((70, 530), sub, font=small, fill="#9aa4b2")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
