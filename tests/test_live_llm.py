import pytest

from fixmytutorial.config import Settings
from fixmytutorial.ledger import Ledger
from fixmytutorial.llm import LLM
from tests.test_llm import Out


@pytest.mark.live
@pytest.mark.parametrize("role", ["triage", "judge", "planner", "repair"])
async def test_live_json_per_role(role, tmp_path):
    s = Settings(data_dir=tmp_path)
    llm = LLM(s, Ledger(tmp_path / "l.db", s))
    out = await llm.call(role, "Return the requested JSON.", 'Return {"ok": true, "n": 3}', Out,
                         run_id="live", public=False, max_tokens=400)
    assert out == Out(ok=True, n=3)
