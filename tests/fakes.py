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
