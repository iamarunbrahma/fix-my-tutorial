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


async def test_stand_in_api_reply_satisfies_generated_text_expectations(settings, tmp_path):
    from fixmytutorial.judge import STUB_MARKER
    client = FakeChatClient(['{"passed": false, "reason": "not a haiku"}'])
    j = Judge(LLM(settings, Ledger(tmp_path / "l.db", settings), client=client))
    out = "This is an offline reply from the Fix My Tutorial stand-in server. Your code reached the API correctly."
    assert STUB_MARKER in out
    res = await j.check(step(description="Prints a haiku about unit tests"), 0, out, run_id="r", public=True)
    assert res.passed and client.calls == []


def test_stub_marker_matches_the_stand_in_server():
    from fixmytutorial.judge import STUB_MARKER
    from fixmytutorial.sandbox import ASSETS_DIR
    assert STUB_MARKER in (ASSETS_DIR / "openai_stub.py").read_text()


async def test_a_cosmetic_difference_from_the_shown_output_goes_to_the_lenient_judge(settings, tmp_path):
    # the review, 16 Oct: Python 3.10 prints "options:" where a 2018 tutorial shows "optional arguments:"
    client = FakeChatClient(['{"passed": true, "reason": "same help text; the heading was renamed"}'])
    j = Judge(LLM(settings, Ledger(tmp_path / "l.db", settings), client=client))
    res = await j.check(step(kind="shell", stdout_contains=["optional arguments:"]), 0,
                        "usage: x [-h]\n\noptions:\n  -h, --help  show this help", run_id="r", public=True)
    assert res.passed and "optional arguments:" in client.calls[0]["messages"][1]["content"]


async def test_shown_output_that_is_really_missing_still_fails(settings, tmp_path):
    client = FakeChatClient(['{"passed": false, "reason": "no accuracy printed"}', RuntimeError("down")])
    j = Judge(LLM(settings, Ledger(tmp_path / "l.db", settings), client=client))
    s = step(stdout_contains=["Accuracy: 0.97"])
    assert not (await j.check(s, 0, "Traceback ... but exit 0", run_id="r", public=True)).passed
    assert not (await j.check(s, 0, "nothing", run_id="r", public=True)).passed  # judge down: the exact check stands
    assert not (await j.check(s, 1, "Accuracy: 0.97", run_id="r", public=True)).passed  # exit code never goes to it
