import pytest

from fixmytutorial.ledger import Ledger
from fixmytutorial.llm import LLM
from fixmytutorial.models import Step, StepResult
from fixmytutorial.triage import heuristic_signature, key_line, triage
from tests.fakes import FakeChatClient

STEP = Step(id="s2", title="t", kind="run_code", content="from sklearn.datasets import load_boston")


@pytest.mark.parametrize("output,category,package,symbol", [
    ("Traceback...\nImportError: cannot import name 'load_boston' from 'sklearn.datasets' (/x.py)",
     "removed_api", "sklearn", "load_boston"),
    ("AttributeError: 'DataFrame' object has no attribute 'append'", "removed_api", None, "DataFrame.append"),
    ("AttributeError: module 'numpy' has no attribute 'float'.", "removed_api", "numpy", "numpy.float"),
    ("ModuleNotFoundError: No module named 'sklearn.externals.joblib'", "renamed_package", "sklearn",
     "sklearn.externals.joblib"),
    ("openai.lib._old_api.APIRemovedInV1: You tried to access openai.ChatCompletion", "removed_api", "openai",
     "openai.ChatCompletion"),
    ("Error [ERR_REQUIRE_ESM]: require() of ES Module /work/node_modules/node-fetch/src/index.js",
     "version_conflict", None, None),
    ("[Fix My Tutorial] Step timed out after 180s", "timeout", None, None),
])
def test_heuristics(output, category, package, symbol):
    sig = heuristic_signature(output, "python")
    assert sig.category == category
    assert sig.package == package and sig.symbol == symbol
    assert sig.search_queries and sig.key_line


def test_key_line_prefers_last_error_line():
    out = "line\nWarning: x\nTraceback (most recent call last):\n  File..\nKeyError: 'a'\n"
    assert key_line(out) == "KeyError: 'a'"


async def test_llm_refines_signature(settings, tmp_path):
    client = FakeChatClient(['{"category": "removed_api", "error_type": "ImportError", "key_line": "",'
                             ' "package": "scikit-learn", "symbol": "load_boston",'
                             ' "search_queries": ["scikit-learn load_boston removed 1.2"]}'])
    llm = LLM(settings, Ledger(tmp_path / "l.db", settings), client=client)
    r = StepResult(exit_code=1, stderr="ImportError: cannot import name 'load_boston' from 'sklearn.datasets'")
    sig = await triage(STEP, r, language="python", llm=llm, run_id="r", public=True)
    assert sig.package == "scikit-learn" and sig.key_line.startswith("ImportError")
    assert client.calls[0]["model"] == settings.model_triage


async def test_llm_failure_falls_back_to_heuristic(settings, tmp_path):
    llm = LLM(settings, Ledger(tmp_path / "l.db", settings), client=FakeChatClient([RuntimeError("x")]))
    r = StepResult(exit_code=1, stderr="AttributeError: module 'numpy' has no attribute 'float'")
    sig = await triage(STEP, r, language="python", llm=llm, run_id="r", public=True)
    assert sig.symbol == "numpy.float"


async def test_timeout_skips_llm(settings, tmp_path):
    client = FakeChatClient([])
    llm = LLM(settings, Ledger(tmp_path / "l.db", settings), client=client)
    r = StepResult(exit_code=124, timed_out=True, stderr="[Fix My Tutorial] Step timed out after 180s")
    sig = await triage(STEP, r, language="python", llm=llm, run_id="r", public=True)
    assert sig.category == "timeout" and client.calls == []
