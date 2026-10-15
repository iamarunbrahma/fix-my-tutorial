"""Everything people share from a run: code changes with reasons and sources, a badge, and a preview image."""
from __future__ import annotations

import difflib
import io
import statistics
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
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
    # Same hues as the site's status pills.
    Verdict.STILL_WORKS: "#059669", Verdict.FIXED: "#7c3aed", Verdict.COULD_NOT_FIX: "#dc2626",
    Verdict.NOT_RUNNABLE: "#71717a", Verdict.ERROR: "#b91c1c",
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


def _day(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def broke_in_text(rec: RunRecord) -> str:
    """One plain paragraph: which release broke the tutorial, how we know, and how long it had worked."""
    b = rec.broke_in
    if b is None:
        return ""
    if b.how == "ran":
        text = (f"Broke in {b.package} {b.first_bad}, released {_day(b.first_bad_date)}. {b.package} {b.last_good} "
                f"still runs this step, and {b.first_bad} fails the same way.")
    else:
        text = (f"Broke in {b.package} {b.first_bad}, released {_day(b.first_bad_date)}, according to the cited "
                "release notes. " + (f"{b.package} {b.last_good} still runs this step." if b.last_good
                                     else "Older releases can't be installed here to check."))
    try:
        published = date.fromisoformat(rec.published) if rec.published else None
    except ValueError:
        published = None
    if published is None:
        return text
    if b.first_bad_date < published:
        return (text + f" That release came out before this page was published ({published:%b %Y}), so the page was "
                "already out of date then.")
    months = (b.first_bad_date.year - published.year) * 12 + b.first_bad_date.month - published.month
    if months < 1:
        return text + f" The tutorial was published in {published:%b %Y}, so it broke within a month of publishing."
    return (text + f" The tutorial was published in {published:%b %Y}, so it worked for about {months} "
            f"month{'s' if months != 1 else ''}.")


def broke_in_source(rec: RunRecord) -> Evidence | None:
    b = rec.broke_in
    if b is None or not b.evidence_id:
        return None
    return next((e for r in rec.repairs for e in r.evidence if e.id == b.evidence_id), None)


def break_stats(entries: list[dict]) -> dict:
    """Rot Index headline numbers about the releases that broke tutorials."""
    dated = [e for e in entries if e.get("broke_in")]
    counts = Counter((e["broke_in"]["package"], e["broke_in"]["version"], e["broke_in"]["date"]) for e in dated)
    releases = [{"package": p, "version": v, "month": f"{date.fromisoformat(d):%b %Y}", "count": n}
                for (p, v, d), n in counts.most_common(5)]
    months, early = [], 0
    for e in dated:
        if not e.get("published"):
            continue
        pub, bad = date.fromisoformat(e["published"]), date.fromisoformat(e["broke_in"]["date"])
        if bad < pub:
            early += 1
        else:
            months.append((bad.year - pub.year) * 12 + bad.month - pub.month)
    median = round(statistics.median(months)) if len(months) >= 3 else None
    return {"releases": releases, "median_months": median, "already_out_of_date": early}


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
    color = VERDICT_COLOR.get(rec.verdict, "#71717a")
    label = escape(f"{left}: {right}")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{lw + rw}" height="20" role="img" aria-label="{label}">'
            f'<title>{label}</title><rect width="{lw}" height="20" fill="#18181b"/>'
            f'<rect x="{lw}" width="{rw}" height="20" fill="{color}"/>'
            f'<g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">'
            f'<text x="{lw / 2}" y="14" text-anchor="middle">{escape(left)}</text>'
            f'<text x="{lw + rw / 2}" y="14" text-anchor="middle">{escape(right)}</text></g></svg>')


def og_png(rec: RunRecord) -> bytes:
    img = Image.new("RGB", (1200, 630), "#09090b")
    d = ImageDraw.Draw(img)
    big, mid, small = (ImageFont.load_default(size=s) for s in (60, 40, 30))
    d.text((70, 60), "Fix My Tutorial", font=small, fill="#a1a1aa")
    title = textwrap.shorten(rec.title or rec.source, 90, placeholder="...")
    for i, line in enumerate(textwrap.wrap(title, 34)[:3]):
        d.text((70, 130 + i * 74), line, font=big, fill="#fafafa")
    label = VERDICT_TEXT.get(rec.verdict, "Checked")
    width = d.textlength(label, font=mid) + 48
    d.rounded_rectangle((70, 420, 70 + width, 492), radius=16, fill=VERDICT_COLOR.get(rec.verdict, "#71717a"))
    d.text((94, 434), label, font=mid, fill="#ffffff")
    n = len(build_changes(rec))
    sub = f"{n} change{'s' if n != 1 else ''}, each with a cited reason" if n else _runtime(rec)
    d.text((70, 530), sub, font=small, fill="#a1a1aa")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
