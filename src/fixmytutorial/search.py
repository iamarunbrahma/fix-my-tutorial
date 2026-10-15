"""Tavily search/extract with credit accounting and a 7-day cache (many tutorials share root causes)."""
from __future__ import annotations

import hashlib
import json
import math

from tavily import AsyncTavilyClient

from .config import Settings
from .ledger import Ledger
from .store import Store

CACHE_TTL_S = 7 * 24 * 3600


def extract_credits(n_urls: int, depth: str) -> int:
    return max(1, math.ceil(n_urls / 5)) * (2 if depth == "advanced" else 1)


def _key(prefix: str, payload: object) -> str:
    return prefix + ":" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]


class SearchClient:
    def __init__(self, settings: Settings, ledger: Ledger, store: Store, client=None):
        self.ledger = ledger
        self.store = store
        self.client = client or AsyncTavilyClient(api_key=settings.tavily_api_key)

    async def search(self, query: str, *, run_id: str, public: bool, max_results: int = 5,
                     include_domains: list[str] | None = None) -> list[dict]:
        key = _key("search", [query, max_results, sorted(include_domains or [])])
        cached = self.store.cache_get(key)
        if cached is not None:
            return cached
        res = self.ledger.reserve_tavily(run_id, public, 1)
        try:
            extra = {"include_domains": include_domains} if include_domains else {}
            resp = await self.client.search(query=query, search_depth="basic", max_results=max_results, **extra)
        except Exception:
            self.ledger.charge_reserved(res, "search", "tavily")  # may have been billed; fail closed
            raise
        self.ledger.commit_tavily(res, 1)
        results = [{"url": r["url"], "title": r.get("title", ""), "content": r.get("content", ""),
                    "score": r.get("score", 0)} for r in resp.get("results", [])]
        self.store.cache_put(key, results, CACHE_TTL_S)
        return results

    async def extract(self, urls: list[str], *, run_id: str, public: bool, query: str | None = None,
                      depth: str = "basic") -> list[dict]:
        if not urls:
            return []
        key = _key("extract", [sorted(urls), query, depth])
        cached = self.store.cache_get(key)
        if cached is not None:
            return cached
        credits = extract_credits(len(urls), depth)
        res = self.ledger.reserve_tavily(run_id, public, credits)
        kwargs = {"extract_depth": depth, "format": "markdown"}
        if query:
            kwargs.update(query=query, chunks_per_source=3)
        try:
            resp = await self.client.extract(urls=urls, **kwargs)
        except Exception:
            self.ledger.charge_reserved(res, "search", "tavily")  # may have been billed; fail closed
            raise
        ok = [{"url": r["url"], "raw_content": r.get("raw_content") or ""} for r in resp.get("results", [])]
        self.ledger.commit_tavily(res, extract_credits(max(1, len(ok)), depth))
        self.store.cache_put(key, ok, CACHE_TTL_S)
        return ok
