from __future__ import annotations

import json as _json
from types import SimpleNamespace

from fixmytutorial.models import ImageRef, StepResult


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
        if isinstance(item, BaseException):
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


class FakeBackend:
    name = "fake"

    def __init__(self, handler=None):
        self.handler = handler or (lambda script, files, image: (0, "", ""))
        self.calls: list[dict] = []
        self._n = 0

    async def base_image(self, language):
        return ImageRef(backend="fake", ref=f"base-{language}")

    async def run(self, image, script, *, files=None, timeout_s=180, save=True, env=None):
        files = files or {}
        self.calls.append({"image": image.ref, "script": script, "files": files, "save": save,
                           "timeout_s": timeout_s})
        out = self.handler(script, files, image)
        exit_code, stdout, stderr = out[0], out[1], out[2]
        timed_out = out[3] if len(out) > 3 else False
        self._n += 1
        return StepResult(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out, duration_s=0.01,
                          image=ImageRef(backend="fake", ref=f"img-{self._n}") if save else None)


def cells_handler(fail_cell=None, outputs=None, other=None):
    """FakeBackend handler: emulates run_cells.py markers; `other(script, files)` handles non-cell steps."""
    outputs = outputs or {}

    def handler(script, files, image):
        if "run_cells.py" in script:
            cells = _json.loads(files["/opt/fmt/cells.json"])
            report_from = script.split("--report-from ")[1].split()[0]
            lines, reporting = [], False
            for c in cells:
                reporting = reporting or c["id"] == report_from
                if not reporting:
                    continue
                lines.append(f'@@FMT {{"cell": "{c["id"]}", "status": "start"}}')
                if c["id"] == fail_cell or (callable(fail_cell) and fail_cell(c)):
                    lines.append(f'@@FMT {{"cell": "{c["id"]}", "status": "error"}}')
                    return (1, "\n".join(lines), "Traceback (most recent call last):\nAttributeError: boom")
                lines.append(outputs.get(c["id"], ""))
                lines.append(f'@@FMT {{"cell": "{c["id"]}", "status": "ok"}}')
            return (0, "\n".join(lines), "")
        return other(script, files) if other else (0, "ok", "")

    return handler


class RoutingChatClient(FakeChatClient):
    """Returns the next canned response for whichever model is requested."""

    def __init__(self, by_model, usage=(100, 50)):
        super().__init__([], usage)
        self.by_model = {k: list(v) for k, v in by_model.items()}

    async def _create(self, **kw):
        self.calls.append(kw)
        queue = self.by_model.get(kw["model"])
        if not queue:
            raise AssertionError(f"unexpected call to {kw['model']}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        usage = SimpleNamespace(prompt_tokens=self._usage[0], completion_tokens=self._usage[1])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=item))], usage=usage)
