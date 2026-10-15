from __future__ import annotations

from types import SimpleNamespace


class FakeChatClient:
    """Mimics openai.AsyncOpenAI enough for LLM.call: client.chat.completions.create(**kw)."""

    def __init__(self, responses, usage=(100, 50)):
        self._responses = list(responses)
        self._usage = usage
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        usage = None if self._usage is None else SimpleNamespace(
            prompt_tokens=self._usage[0], completion_tokens=self._usage[1])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=item))], usage=usage)


class FakeTavily:
    """Mimics tavily.AsyncTavilyClient.search/extract."""

    def __init__(self, search_results=None, extract_results=None):
        self.search_results = search_results or []
        self.extract_results = extract_results or {}
        self.search_calls: list[dict] = []
        self.extract_calls: list[dict] = []

    async def search(self, query, **kw):
        self.search_calls.append({"query": query, **kw})
        res = self.search_results.get(query, []) if isinstance(self.search_results, dict) else self.search_results
        return {"query": query, "results": res}

    async def extract(self, urls, **kw):
        urls = [urls] if isinstance(urls, str) else list(urls)
        self.extract_calls.append({"urls": urls, **kw})
        ok = [{"url": u, "raw_content": self.extract_results[u]} for u in urls if u in self.extract_results]
        failed = [{"url": u, "error": "not found"} for u in urls if u not in self.extract_results]
        return {"results": ok, "failed_results": failed}
