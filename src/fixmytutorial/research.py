"""Find the changelog, migration note or issue that explains a failure, as quotable evidence."""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

import httpx

from .ledger import BudgetExceeded
from .models import Evidence, FailureSignature

PREFERRED_DOMAINS = (
    "docs.python.org", "pandas.pydata.org", "scikit-learn.org", "numpy.org", "matplotlib.org", "github.com",
    "stackoverflow.com", "pypi.org", "readthedocs.io", "nodejs.org", "npmjs.com", "developer.mozilla.org",
    "python.langchain.com", "langchain.com", "platform.openai.com", "flask.palletsprojects.com", "expressjs.com",
    "react.dev", "nextjs.org", "fastapi.tiangolo.com", "docs.djangoproject.com", "seaborn.pydata.org",
)


def domain_rank(url: str) -> int:
    host = urlparse(url).hostname or ""
    return 0 if any(host == d or host.endswith("." + d) for d in PREFERRED_DOMAINS) else 1


HEADING_RE = re.compile(r"^#{1,6}\s")
MARKER_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?)", re.M)  # Markdown heading and quote markers
MIGRATION_WORDS = ("removed", "deprecat", "instead", "replac", "renamed", "migrat", "no longer", "use ")


def symbol_term(sig: FailureSignature) -> str | None:
    term = sig.symbol.split(".")[-1].lower() if sig.symbol else ""
    return term if len(term) >= 3 else None


def best_passage(text: str, sig: FailureSignature, limit: int = 600) -> str:
    if not text:
        return ""
    terms = {t.split(".")[-1].lower() for t in (sig.symbol, sig.package) if t}
    terms |= {w.split(".")[-1].lower() for w in re.findall(r"[A-Za-z_][\w\.]{3,}", sig.key_line)[:5]}
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        return re.sub(r"\s+", " ", text)[:limit]
    paras = [p for p in paras if not HEADING_RE.match(p)] or paras  # a heading alone explains nothing

    def score(p: str) -> int:  # distinct terms present, plus a bonus for saying what changed
        low = p.lower()
        hits = sum(t in low for t in terms if t)
        return hits + (2 if hits and any(w in low for w in MIGRATION_WORDS) else 0)

    best = MARKER_RE.sub("", max(paras, key=score))
    return re.sub(r"\s+", " ", best).strip()[:limit]


def closest_capture(stamps: list[str], when: str | None) -> str | None:
    """The capture nearest the tutorial's date (YYYYMMDD), or the newest one when the date is unknown."""
    if not stamps:
        return None
    if not when:
        return max(stamps)
    day = date(int(when[:4]), int(when[4:6]), int(when[6:8]))
    return min(stamps, key=lambda ts: abs((date(int(ts[:4]), int(ts[4:6]), int(ts[6:8])) - day).days))


async def wayback(url: str, when: str | None) -> tuple[str, str] | None:
    """The Wayback Machine's saved copy of a link, closest to `when` (YYYYMMDD): (raw copy URL, capture date).
    Lists the captures and picks one here: the archive's own closest-first sort takes about 30 seconds."""
    params = {"url": url, "output": "json", "filter": "statuscode:200", "limit": "200", "fl": "timestamp"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            rows = (await client.get("https://web.archive.org/cdx/search/cdx", params=params)).json()
        ts = closest_capture([r[0] for r in rows[1:]], when)
    except Exception:  # the archive is unreachable or answered oddly: no evidence rather than a guess
        return None
    if ts is None:
        return None
    return f"https://web.archive.org/web/{ts}id_/{url}", f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"


async def research(sig: FailureSignature, *, search, run_id: str, public: bool, id_prefix: str = "e",
                   max_items: int = 6, links: list[str] = (), when: str | None = None,
                   archive=wayback) -> list[Evidence]:
    if sig.category == "timeout":
        return []
    queries = sig.search_queries[:3] or ([sig.key_line[:200]] if sig.key_line else [])
    seen: dict[str, dict] = {}
    for q in queries:
        try:
            results = await search.search(q, run_id=run_id, public=public)
        except BudgetExceeded:
            raise
        except Exception:
            continue
        for r in results:
            if urlparse(r["url"]).scheme in ("http", "https"):  # quotes become clickable links
                seen.setdefault(r["url"], r)
    ranked = sorted(seen.values(), key=lambda r: (domain_rank(r["url"]), -float(r.get("score") or 0)))
    extracted: dict[str, str] = {}
    top = ranked[:3]
    if top:
        focus = " ".join(x for x in (sig.symbol, sig.package, sig.error_type) if x) or sig.key_line[:120]
        try:
            ex = await search.extract([r["url"] for r in top], run_id=run_id, public=public, query=focus)
        except BudgetExceeded:
            raise
        except Exception:
            ex = []
        extracted = {e["url"]: e["raw_content"] for e in ex}
    items: list[Evidence] = []
    sym = symbol_term(sig)
    for r in ranked[:max_items]:
        quote = best_passage(extracted.get(r["url"]) or r.get("content", ""), sig)
        if quote and (sym is None or sym in quote.lower()):  # a quote that never names the symbol proves nothing
            items.append(Evidence(id=f"{id_prefix}{len(items) + 1}", url=r["url"],
                                  title=(r.get("title") or r["url"])[:200], quote=quote))
    if sig.category == "dead_resource":  # the tutorial's own dead links, as the archive saved them
        for link in links[:3]:
            if found := await archive(link, when):
                raw, day = found
                items.append(Evidence(id=f"{id_prefix}{len(items) + 1}", url=raw, title=f"Archived copy of {link}"[:200],
                                      quote=f"The Wayback Machine saved {link} on {day}. That copy is at {raw}"))
    return items
