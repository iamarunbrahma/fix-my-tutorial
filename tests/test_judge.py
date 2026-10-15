from fixmytutorial.judge import Judge, deterministic_check
from fixmytutorial.ledger import Ledger
from fixmytutorial.llm import LLM
from fixmytutorial.models import Expectation, Step
from tests.fakes import FakeChatClient


def step(kind="run_code", **expect):
    return Step(id="s1", title="t", kind=kind, content="print(1)", expect=Expectation(**expect))


def test_deterministic_rules():
    assert deterministic_check(step(), 1, "") is False
    assert deterministic_check(step(stdout_contains=["42"]), 0, "41") is False
    assert deterministic_check(step(stdout_contains=["42"]), 0, "x 42") is True
    assert deterministic_check(step(description="prints accuracy"), 0, "0.93") is None
    assert deterministic_check(step(kind="shell", description="installs"), 0, "") is True


async def test_fuzzy_uses_nano(settings, tmp_path):
    client = FakeChatClient(['{"passed": false, "reason": "no accuracy printed"}'])
    j = Judge(LLM(settings, Ledger(tmp_path / "l.db", settings), client=client))
    res = await j.check(step(description="prints accuracy"), 0, "", run_id="r", public=True)
    assert not res.passed and client.calls[0]["model"] == settings.model_judge


async def test_judge_error_falls_back_to_pass(settings, tmp_path):
    client = FakeChatClient([RuntimeError("down")])
    j = Judge(LLM(settings, Ledger(tmp_path / "l.db", settings), client=client))
    res = await j.check(step(description="prints accuracy"), 0, "0.9", run_id="r", public=True)
    assert res.passed


async def test_no_llm_passes_fuzzy_on_exit_zero():
    assert (await Judge(None).check(step(description="x"), 0, "", run_id="r", public=True)).passed


def test_stdout_match_ignores_whitespace_differences():
    assert deterministic_check(step(stdout_contains=["   name  score"]), 0, "  name  score\n0  ana     91") is True
    assert deterministic_check(step(stdout_contains=["name score"]), 0, "name\nscores") is True
    assert deterministic_check(step(stdout_contains=["name score"]), 0, "namescore") is False
