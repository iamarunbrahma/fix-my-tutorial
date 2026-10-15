"""Find the changelog, migration note or issue that explains a failure, as quotable evidence."""
from __future__ import annotations

import re
from urllib.parse import urlparse

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


def best_passage(text: str, sig: FailureSignature, limit: int = 600) -> str:
    if not text:
        return ""
    terms = [t.split(".")[-1].lower() for t in (sig.symbol, sig.package) if t]
    terms += [w.split(".")[-1].lower() for w in re.findall(r"[A-Za-z_][\w\.]{3,}", sig.key_line)[:5]]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        return re.sub(r"\s+", " ", text)[:limit]
    best = max(paras, key=lambda p: sum(p.lower().count(t) for t in terms if t))
    return re.sub(r"\s+", " ", best)[:limit]


async def research(sig: FailureSignature, *, search, run_id: str, public: bool, id_prefix: str = "e",
                   max_items: int = 6) -> list[Evidence]:
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
    for r in ranked[:max_items]:
        quote = best_passage(extracted.get(r["url"]) or r.get("content", ""), sig)
        if quote:
            items.append(Evidence(id=f"{id_prefix}{len(items) + 1}", url=r["url"],
                                  title=(r.get("title") or r["url"])[:200], quote=quote))
    return items
