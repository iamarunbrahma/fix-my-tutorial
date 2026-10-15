import pytest
from pydantic import BaseModel

from fixmytutorial.ledger import BudgetExceeded, Ledger
from fixmytutorial.llm import LLM, LLMOutputError, extract_json
from tests.fakes import FakeChatClient


class Out(BaseModel):
    ok: bool
    n: int


def make_llm(settings, tmp_path, responses, **kw):
    led = Ledger(tmp_path / "l.db", settings)
    client = FakeChatClient(responses, **kw)
    return LLM(settings, led, client=client), client, led


def test_extract_json_strips_think_and_fences():
    raw = '<think>hmm {"no": 1}</think>\n```json\n{"ok": true, "n": 2}\n```'
    assert extract_json(raw) == '{"ok": true, "n": 2}'


def test_extract_json_skips_leading_prose():
    assert extract_json('Sure! {"ok": false, "n": 0}') == '{"ok": false, "n": 0}'


async def test_call_parses_and_records_spend(settings, tmp_path):
    llm, client, led = make_llm(settings, tmp_path, ['{"ok": true, "n": 3}'])
    out = await llm.call("triage", "sys", "user", Out, run_id="r", public=True)
    assert out == Out(ok=True, n=3)
    kw = client.calls[0]
    assert kw["model"] == settings.model_triage
    assert kw["response_format"]["type"] == "json_schema"
    assert kw["response_format"]["json_schema"]["name"] == "Out"
    assert led.totals("r").run_usd > 0


async def test_json_object_mode_for_configured_roles(settings, tmp_path):
    s = settings.model_copy(update={"json_object_roles": "triage"})
    led = Ledger(tmp_path / "l.db", s)
    client = FakeChatClient(['{"ok": true, "n": 1}'])
    await LLM(s, led, client=client).call("triage", "sys", "u", Out, run_id="r", public=False)
    assert client.calls[0]["response_format"] == {"type": "json_object"}


async def test_retry_once_on_invalid_json(settings, tmp_path):
    llm, client, _ = make_llm(settings, tmp_path, ['{"ok": "maybe"}', '{"ok": true, "n": 1}'])
    out = await llm.call("planner", "sys", "u", Out, run_id="r", public=False)
    assert out.n == 1
    assert "invalid" in client.calls[1]["messages"][-1]["content"]


async def test_gives_up_after_two_invalid(settings, tmp_path):
    llm, _, _ = make_llm(settings, tmp_path, ["nope", "still nope"])
    with pytest.raises(LLMOutputError):
        await llm.call("planner", "sys", "u", Out, run_id="r", public=False)


async def test_budget_exceeded_stops_before_api_call(settings, tmp_path):
    s = settings.model_copy(update={"cap_run_usd": 0.000001})
    led = Ledger(tmp_path / "l.db", s)
    client = FakeChatClient(['{"ok": true, "n": 1}'])
    with pytest.raises(BudgetExceeded):
        await LLM(s, led, client=client).call("repair", "sys", "u", Out, run_id="r", public=True)
    assert client.calls == []


async def test_missing_usage_falls_back_to_estimate(settings, tmp_path):
    llm, _, led = make_llm(settings, tmp_path, ['{"ok": true, "n": 1}'], usage=None)
    await llm.call("judge", "sys", "u", Out, run_id="r", public=False)
    assert led.totals("r").run_usd > 0


def make_exc(cls, msg="x"):
    e = cls.__new__(cls)
    Exception.__init__(e, msg)
    return e


def test_estimate_is_an_upper_bound_on_tokens():
    from fixmytutorial.llm import estimate_tokens
    assert estimate_tokens("abcd") >= 4
    assert estimate_tokens("naïve 日本語") >= len("naïve 日本語".encode())


def test_default_client_has_no_hidden_retries(settings, tmp_path):
    s = settings.model_copy(update={"nebius_api_key": "k"})
    assert LLM(s, Ledger(tmp_path / "l.db", s)).client.max_retries == 0


async def test_unknown_error_is_charged_pessimistically(settings, tmp_path):
    llm, _, led = make_llm(settings, tmp_path, [RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        await llm.call("repair", "sys", "u", Out, run_id="r", public=False)
    assert led.totals("r").run_usd > 0


async def test_rate_limit_is_not_charged_and_is_retried(settings, tmp_path):
    import openai
    llm, client, led = make_llm(settings, tmp_path, [make_exc(openai.RateLimitError), '{"ok": true, "n": 1}'])
    llm._backoff = (0, 0)
    out = await llm.call("triage", "sys", "u", Out, run_id="r", public=False)
    assert out.n == 1 and len(client.calls) == 2
    assert led.totals("r").run_usd == pytest.approx(led.price_usd("triage", 100, 50))


async def test_timeout_is_charged_then_retried(settings, tmp_path):
    import openai
    llm, client, led = make_llm(settings, tmp_path, [make_exc(openai.APITimeoutError), '{"ok": true, "n": 1}'])
    llm._backoff = (0, 0)
    await llm.call("triage", "sys", "u", Out, run_id="r", public=False)
    assert len(client.calls) == 2
    assert led.totals("r").run_usd > led.price_usd("triage", 100, 50)


async def test_bad_request_is_not_charged_or_retried(settings, tmp_path):
    import openai
    llm, client, led = make_llm(settings, tmp_path, [make_exc(openai.BadRequestError)])
    with pytest.raises(openai.BadRequestError):
        await llm.call("triage", "sys", "u", Out, run_id="r", public=False)
    assert len(client.calls) == 1 and led.totals("r").run_usd == 0


async def test_missing_api_key_fails_at_call_time_not_at_startup(settings, tmp_path):
    led = Ledger(tmp_path / "l.db", settings)
    llm = LLM(settings.model_copy(update={"nebius_api_key": ""}), led)
    with pytest.raises(RuntimeError, match="NEBIUS_API_KEY"):
        await llm.call("triage", "s", "u", Out, run_id="r", public=False)
    assert led.totals("r").run_usd == 0


def test_extract_json_ignores_trailing_text_after_first_object():
    raw = '{"ok": true, "n": 3}"\n Here it is again: `{"ok": true, "n": 3}`'
    assert extract_json(raw) == '{"ok": true, "n": 3}'


def test_extract_json_skips_a_broken_brace_before_the_real_object():
    raw = 'Note {not json} then {"ok": false, "n": 1} done'
    assert extract_json(raw) == '{"ok": false, "n": 1}'


async def test_cancelled_call_is_charged_and_saved(settings, tmp_path):
    import asyncio
    llm, _, led = make_llm(settings, tmp_path, [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await llm.call("repair", "sys", "u", Out, run_id="r", public=True)
    assert [row["run_id"] for row in led.export_rows()] == ["r"]
    assert led.totals("r").run_usd == pytest.approx(sum(row["usd"] for row in led.export_rows()))


async def test_malformed_reply_is_charged(settings, tmp_path):
    from types import SimpleNamespace

    class NoChoices:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kw):
            return SimpleNamespace(choices=[], usage=None)

    led = Ledger(tmp_path / "l.db", settings)
    with pytest.raises(IndexError):
        await LLM(settings, led, client=NoChoices()).call("triage", "sys", "u", Out, run_id="r", public=True)
    assert len(led.export_rows()) == 1 and led.totals("r").run_usd > 0
