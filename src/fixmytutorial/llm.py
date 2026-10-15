"""Token Factory (OpenAI-compatible) client with JSON-schema outputs and the spend ledger in front."""
from __future__ import annotations

import json
import re
from typing import TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from .config import Settings
from .ledger import Ledger

T = TypeVar("T", bound=BaseModel)

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*$", re.M)


class LLMOutputError(Exception):
    pass


def extract_json(text: str) -> str:
    text = _THINK.sub("", text or "")
    text = _FENCE.sub("", text).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if starts:
        text = text[min(starts):]
    end = max(text.rfind("}"), text.rfind("]"))
    if end != -1:
        text = text[: end + 1]
    return text.strip()


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)


class LLM:
    def __init__(self, settings: Settings, ledger: Ledger, client=None):
        self.settings = settings
        self.ledger = ledger
        self.client = client or AsyncOpenAI(
            base_url=settings.nebius_base_url, api_key=settings.nebius_api_key, max_retries=3, timeout=240
        )

    def _response_format(self, role: str, schema: type[BaseModel]) -> dict:
        if self.settings.uses_json_object(role):
            return {"type": "json_object"}
        return {"type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}}

    async def call(self, role: str, system: str, user: str, schema: type[T], *, run_id: str, public: bool,
                   max_tokens: int = 4096, temperature: float = 0.2) -> T:
        model = self.settings.model_for(role)
        system_full = (
            f"{system}\n\nRespond with one JSON object that matches this JSON schema. No prose, no markdown.\n"
            f"{json.dumps(schema.model_json_schema())}"
        )
        messages = [{"role": "system", "content": system_full}, {"role": "user", "content": user}]
        last_error: Exception | None = None
        for _ in range(2):
            prompt_est = estimate_tokens("".join(m["content"] for m in messages))
            res = self.ledger.reserve_llm(run_id, public, role, prompt_est, max_tokens)
            try:
                resp = await self.client.chat.completions.create(
                    model=model, messages=messages, max_tokens=max_tokens, temperature=temperature,
                    response_format=self._response_format(role, schema),
                )
            except Exception:
                self.ledger.release(res)
                raise
            content = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            pt = getattr(usage, "prompt_tokens", None) or prompt_est
            ct = getattr(usage, "completion_tokens", None) or estimate_tokens(content)
            self.ledger.commit_llm(res, role, model, pt, ct)
            try:
                return schema.model_validate_json(extract_json(content))
            except ValidationError as e:
                last_error = e
                messages = messages + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"That JSON was invalid: {str(e)[:1500]}\n"
                                                "Reply again with only the corrected JSON object."},
                ]
        raise LLMOutputError(f"{role} returned invalid JSON twice: {last_error}")
