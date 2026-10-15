"""A note for the tutorial's author: what broke, the fix that works today and its sources, ready to file as a
GitHub issue (the visitor files it from their own account; we never post anything) or to paste anywhere else."""
from __future__ import annotations

import re
from urllib.parse import urlencode, urlparse

from .models import RunRecord, Verdict
from .report import broke_in_text, build_changes

NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
NOT_REPOS = {"orgs", "organizations", "topics", "marketplace", "features", "settings", "sponsors", "apps", "login",
             "explore", "collections", "trending", "search", "about", "pricing", "enterprise", "notifications", "new"}
FENCE = re.compile(r"^(`{3,})")


def github_repo_for(url: str) -> tuple[str, str] | None:
    """The GitHub repository a tutorial page lives in, when its link says so."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if u.scheme not in ("http", "https"):
        return None
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    if host in ("github.com", "www.github.com", "raw.githubusercontent.com"):
        pair = parts[:2]
    elif host in ("colab.research.google.com", "nbviewer.org", "nbviewer.jupyter.org") and parts[:1] == ["github"]:
        pair = parts[1:3]
    elif host.endswith(".github.io") and host.count(".") == 2:  # GitHub Pages: owner.github.io/repo
        first = parts[0] if parts and not parts[0].lower().endswith((".html", ".htm")) else host
        pair = [host.removesuffix(".github.io"), first]
    else:
        return None
    if len(pair) < 2:
        return None
    owner, repo = pair[0], pair[1].removesuffix(".git")
    if host.endswith("github.com") and owner.lower() in NOT_REPOS:
        return None
    if not (NAME.match(owner) and NAME.match(repo)):
        return None
    return owner, repo


def _fence(text: str) -> str:
    """A code fence longer than any backtick run inside the text, so tutorial code can't close it early."""
    return "`" * max([3] + [len(run) + 1 for run in re.findall(r"`{3,}", text)])


def _block(text: str, lang: str = "") -> str:
    f = _fence(text)
    return f"{f}{lang}\n{text.rstrip()}\n{f}"


def author_note(rec: RunRecord, report_url: str) -> tuple[str, str]:
    """Issue title and Markdown body for a tutorial that broke (fixed or not)."""
    plan = rec.final_plan or rec.plan
    runtime = (("Python " if plan.language == "python" else "Node.js ") + plan.runtime) if plan else "today's runtime"
    checked = (rec.finished_at or rec.created_at).strftime("%d %b %Y")
    first = rec.repairs[0] if rec.repairs else None
    title = (f"This tutorial fails today: {first.signature.key_line}"[:150] if first and first.signature.key_line
             else "This tutorial no longer runs as written")
    parts = [f"I ran this tutorial step by step in a fresh sandbox with today's package versions ({runtime}, "
             f"checked {checked}). One step no longer works as written."]
    if first:
        steps = {s.id: s.title for s in (rec.plan.steps if rec.plan else [])}
        parts.append(f'**What breaks:** "{steps.get(first.step_id, first.step_id)}" fails with:\n\n'
                     + _block(first.signature.key_line or "an error"))
    elif rec.reason:
        parts.append(f"**What we found:** {rec.reason}")
    when = broke_in_text(rec)
    if when:
        parts.append(f"**When it broke:** {when}")
    changes = build_changes(rec) if rec.verdict == Verdict.FIXED else []
    if changes:
        parts.append("**A fix that works today.** We re-ran the whole tutorial from scratch with it:")
        for c in changes:
            lines = (["+" + ln for ln in c.after.splitlines()] if c.added else
                     [ln for ln in c.diff.splitlines() if not ln.startswith(("---", "+++"))])
            head = f"**{c.title}:** {c.reason}" if c.reason else f"**{c.title}**"
            parts.append(head + "\n\n" + _block("\n".join(lines), "diff"))
    else:
        parts.append("We couldn't find a fix that passes yet. These sources explain the error:")
    seen, sources = set(), []
    for c in changes:
        for e in c.citations:
            if e.url not in seen:
                seen.add(e.url)
                sources.append(e)
    if not changes:
        for r in rec.repairs:
            for e in r.evidence:
                if e.url not in seen:
                    seen.add(e.url)
                    sources.append(e)
    if sources:
        parts.append(("Sources:\n" if changes else "") + "\n".join(f"- [{e.title}]({e.url})" for e in sources[:5]))
    parts.append(f"Full report: {report_url}")
    return title, "\n\n".join(parts)


def _close_fences(text: str) -> str:
    open_fence = None
    for line in text.splitlines():
        m = FENCE.match(line)
        if not m:
            continue
        if open_fence is None:
            open_fence = m.group(1)
        elif line.strip() == open_fence:
            open_fence = None
    return f"{text}\n{open_fence}" if open_fence else text


def issue_url(owner: str, repo: str, title: str, body: str, report_url: str, limit: int = 7000) -> str:
    """GitHub's new-issue page, filled in. Long bodies are cut on a line boundary, keeping the report link."""
    base = f"https://github.com/{owner}/{repo}/issues/new?"

    def build(b: str) -> str:
        return base + urlencode({"title": title, "body": b})

    if len(build(body)) <= limit:
        return build(body)
    lines = body.splitlines()
    tail = f"\n\n(Shortened to fit. Full report: {report_url})"

    def cut(n: int) -> str:
        return _close_fences("\n".join(lines[:n])) + tail

    lo, hi = 0, len(lines)  # largest n whose cut still fits
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(build(cut(mid))) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return build(cut(lo).strip())
