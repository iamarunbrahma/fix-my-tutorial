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


async def test_api_error_releases_reservation(settings, tmp_path):
    llm, _, led = make_llm(settings, tmp_path, [RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        await llm.call("repair", "sys", "u", Out, run_id="r", public=False)
    assert led.totals("r").run_usd == 0


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
