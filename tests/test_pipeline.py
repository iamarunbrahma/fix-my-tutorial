import asyncio
import json

from fixmytutorial.events import EventBus
from fixmytutorial.ingest import Source
from fixmytutorial.ledger import Ledger
from fixmytutorial.llm import LLM
from fixmytutorial.models import RunRecord, RunStatus, StepStatus, Verdict, utcnow
from fixmytutorial.pipeline import Pipeline
from fixmytutorial.search import SearchClient
from fixmytutorial.store import Store
from tests.fakes import FakeBackend, FakeTavily, RoutingChatClient, cells_handler

MD = "# Pandas rows\n\n```bash\npip install pandas\n```\n\n```python\ndf = df.append(row)\n```\n"
PLAN = json.dumps({"language": "python", "runnable": True, "steps": [
    {"id": "a", "title": "Install pandas", "kind": "shell", "content": "pip install pandas"},
    {"id": "b", "title": "Make frame", "kind": "run_code", "content": "import pandas as pd\ndf = pd.DataFrame()"},
    {"id": "c", "title": "Append a row", "kind": "run_code", "content": "df = df.append({'a': 1}, ignore_index=True)"},
]})
SIG = json.dumps({"category": "removed_api", "error_type": "AttributeError", "package": "pandas",
                  "symbol": "DataFrame.append", "key_line": "AttributeError: 'DataFrame' object has no attribute 'append'",
                  "search_queries": ["pandas DataFrame.append removed 2.0"]})
FIX = json.dumps({"candidates": [{"strategy": "migrate", "rationale": "use concat", "edits": [
    {"op": "replace", "step_id": "s3", "new_content": "df = pd.concat([df, pd.DataFrame([{'a': 1}])])",
     "reason": "pandas 2.0 removed DataFrame.append", "evidence_ids": ["r1e1"]}]}]})
BAD_FIX = FIX.replace("pd.concat", "df.append")
SUMMARY = json.dumps({"summary": "Ran with pandas 2. DataFrame.append was removed, so we switched to pd.concat."})
TAVILY = FakeTavily([{"url": "https://pandas.pydata.org/whatsnew/v2.0.0", "title": "What's new in 2.0.0",
                      "content": "Removed DataFrame.append; use concat", "score": 0.9}],
                    {"https://pandas.pydata.org/whatsnew/v2.0.0": "DataFrame.append was removed. Use pandas.concat."})


def needs_append(cell):
    return "df.append(" in cell["code"]


def build(settings, tmp_path, by_model, handler=None, **overrides):
    s = settings.model_copy(update=overrides)
    store = Store(tmp_path / "s.db")
    ledger = Ledger(tmp_path / "l.db", s)
    bus = EventBus(store, ping_s=0.05)
    llm = LLM(s, ledger, client=RoutingChatClient(by_model))
    search = SearchClient(s, ledger, store, client=TAVILY)
    backend = FakeBackend(handler or cells_handler(fail_cell=needs_append))
    p = Pipeline(settings=s, llm=llm, search=search, backend=backend, store=store, ledger=ledger, bus=bus)
    return p, store, backend, s


def new_record():
    return RunRecord(id="run1", source="pasted-markdown", created_at=utcnow())


def events(store):
    return [e for _, e in store.events_after("run1", 0)]


async def test_pipeline_fixes_and_replays(settings, tmp_path):
    p, store, backend, s = build(settings, tmp_path, {
        settings.model_planner: [PLAN, SUMMARY], settings.model_triage: [SIG], settings.model_repair: [FIX]})
    rec = await p.run(new_record(), Source(markdown=MD))
    assert rec.verdict == Verdict.FIXED and rec.status == RunStatus.DONE
    assert "pd.concat" in next(st.content for st in rec.final_plan.steps if st.id == "s3")
    assert rec.repairs[0].winner.strategy == "migrate" and rec.repairs[0].evidence[0].id == "r1e1"
    assert rec.outcomes["s3"].status == StepStatus.REPAIRED
    assert rec.summary.startswith("Ran with pandas 2") and rec.cost_usd > 0 and rec.tavily_credits >= 2
    types = [e["type"] for e in events(store)]
    for t in ["plan", "diagnosis", "evidence", "candidates", "candidate_result"]:
        assert t in types
    assert {"type": "stage", "stage": "replaying"} in events(store)
    assert types[-2:] == ["verdict", "done"]
    assert store.get_run("run1").verdict == Verdict.FIXED


async def test_pipeline_still_works(settings, tmp_path):
    p, store, backend, _ = build(settings, tmp_path, {settings.model_planner: [PLAN, SUMMARY]},
                                 handler=cells_handler())
    rec = await p.run(new_record(), Source(markdown=MD))
    assert rec.verdict == Verdict.STILL_WORKS and rec.repairs == [] and rec.final_plan is not None
    assert all(o.status == StepStatus.PASSED for o in rec.outcomes.values())


async def test_pipeline_could_not_fix(settings, tmp_path):
    p, store, _, _ = build(settings, tmp_path, {
        settings.model_planner: [PLAN, SUMMARY], settings.model_triage: [SIG], settings.model_repair: [BAD_FIX]},
        repair_rounds=1)
    rec = await p.run(new_record(), Source(markdown=MD))
    assert rec.verdict == Verdict.COULD_NOT_FIX and "append" in rec.reason
    assert rec.status == RunStatus.DONE


async def test_pipeline_not_runnable(settings, tmp_path):
    plan = json.dumps({"language": "python", "runnable": False, "not_runnable_reason": "Needs a GPU.", "steps": []})
    p, store, backend, _ = build(settings, tmp_path, {settings.model_planner: [plan]})
    rec = await p.run(new_record(), Source(markdown=MD))
    assert rec.verdict == Verdict.NOT_RUNNABLE and rec.reason == "Needs a GPU." and backend.calls == []


async def test_pipeline_ingest_failure_is_friendly(settings, tmp_path):
    p, store, _, _ = build(settings, tmp_path, {})
    rec = await p.run(new_record(), Source(url="file:///etc/passwd"))
    assert rec.verdict == Verdict.ERROR and rec.status == RunStatus.FAILED
    assert rec.reason == "Only http and https links are supported."
    assert [e["type"] for e in events(store)][-2:] == ["verdict", "done"]


async def test_pipeline_budget_exceeded_mid_run(settings, tmp_path):
    p, store, _, _ = build(settings, tmp_path, {
        settings.model_planner: [PLAN], settings.model_triage: [SIG], settings.model_repair: [FIX]},
        cap_run_usd=0.015)
    rec = await p.run(new_record(), Source(markdown=MD))
    assert rec.verdict == Verdict.ERROR and "spending limit" in rec.reason
    assert rec.outcomes["s1"].status == StepStatus.PASSED
    last = {}
    for _, ev in store.events_after(rec.id, 0):
        if ev["type"] == "step":
            last[ev["step_id"]] = ev["status"]
    assert "repairing" not in last.values()  # the page must not leave a step spinning after the run stopped


async def test_wall_clock_limit(settings, tmp_path):
    p, store, backend, _ = build(settings, tmp_path, {settings.model_planner: [PLAN]}, run_wall_clock_s=0.2)
    original = backend.run

    async def slow(*a, **kw):
        await asyncio.sleep(1)
        return await original(*a, **kw)

    backend.run = slow
    rec = await p.run(new_record(), Source(markdown=MD))
    assert rec.verdict == Verdict.ERROR and "longer than" in rec.reason
