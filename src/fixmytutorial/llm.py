"""Token Factory (OpenAI-compatible) client with JSON-schema outputs and the spend ledger in front."""
from __future__ import annotations

import asyncio
import json
import re
from typing import TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from .config import Settings
from .ledger import Ledger

T = TypeVar("T", bound=BaseModel)

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*$", re.M)


# Errors that are never billed (request rejected before any generation): release the reservation.
UNBILLED = (openai.RateLimitError, openai.BadRequestError, openai.AuthenticationError, openai.PermissionDeniedError,
            openai.NotFoundError, openai.UnprocessableEntityError, openai.ConflictError)
# Errors worth retrying. Timeouts and dropped connections may still have been billed, so they are charged in full.
RETRYABLE = (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError)


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
    """Upper bound on tokens: every token covers at least one UTF-8 byte."""
    return len((text or "").encode("utf-8")) + 4


def estimate_prompt(messages: list[dict]) -> int:
    return sum(estimate_tokens(m["content"]) + 16 for m in messages) + 64


class LLM:
    def __init__(self, settings: Settings, ledger: Ledger, client=None):
        self.settings = settings
        self.ledger = ledger
        self._backoff: tuple[float, ...] = (2.0, 6.0)  # retry delays; each attempt is reserved and accounted
        self._client = client

    @property
    def client(self):
        """Created on first use, so the site can start and serve reports without an API key."""
        if self._client is None:
            if not self.settings.nebius_api_key:
                raise RuntimeError("NEBIUS_API_KEY is not set, so Nemotron calls are unavailable.")
            self._client = AsyncOpenAI(base_url=self.settings.nebius_base_url, api_key=self.settings.nebius_api_key,
                                       max_retries=0, timeout=240)
        return self._client

    async def _complete(self, role, model, messages, schema, run_id, public, max_tokens, temperature) -> str:
        """One completion with retries. Every attempt is reserved first; an attempt that may have been billed is
        charged in full even when it fails, so the spend guard never under-counts."""
        attempts = len(self._backoff) + 1
        for attempt in range(attempts):
            prompt_est = estimate_prompt(messages)
            res = self.ledger.reserve_llm(run_id, public, role, prompt_est, max_tokens)
            try:
                resp = await self.client.chat.completions.create(
                    model=model, messages=messages, max_tokens=max_tokens, temperature=temperature,
                    response_format=self._response_format(role, schema),
                )
            except Exception as e:
                if isinstance(e, UNBILLED):
                    self.ledger.release(res)
                else:
                    self.ledger.charge_reserved(res, role, model)
                if isinstance(e, RETRYABLE) and attempt < attempts - 1:
                    await asyncio.sleep(self._backoff[attempt])
                    continue
                raise
            content = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            pt = getattr(usage, "prompt_tokens", None) or prompt_est
            ct = getattr(usage, "completion_tokens", None) or estimate_tokens(content)
            self.ledger.commit_llm(res, role, model, pt, ct)
            return content
        raise RuntimeError("unreachable")

    def _response_format(self, role: str, schema: type[BaseModel]) -> dict:
        if self.settings.uses_json_object(role):
            return {"type": "json_object"}
        return {"type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}}

    async def call(self, role: str, system: str, user: str, schema: type[T], *, run_id: str, public: bool,
                   max_tokens: int = 4096, temperature: float = 0.2) -> T:
        model = self.settings.model_for(role)
        self.client  # noqa: B018 - fail before reserving any budget if the API key is missing
        system_full = (
            f"{system}\n\nRespond with one JSON object that matches this JSON schema. No prose, no markdown.\n"
            f"{json.dumps(schema.model_json_schema())}"
        )
        messages = [{"role": "system", "content": system_full}, {"role": "user", "content": user}]
        last_error: Exception | None = None
        for _ in range(2):
            content = await self._complete(role, model, messages, schema, run_id, public, max_tokens, temperature)
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
