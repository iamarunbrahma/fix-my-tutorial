import json

from fixmytutorial.executor import ExecState, Executor
from fixmytutorial.judge import Judge
from fixmytutorial.ledger import Ledger
from fixmytutorial.llm import LLM
from fixmytutorial.models import Candidate, Edit, Evidence, FailureSignature, ImageRef, RunPlan, Step
from fixmytutorial.repair import (
    RaceResult, Repairer, apply_edits, first_changed_unit, pick, validate_candidate,
)
from tests.fakes import FakeBackend, FakeChatClient, cells_handler

EVID = [Evidence(id="r1e1", url="https://pandas.pydata.org/whatsnew/v2.0", title="2.0",
                 quote="DataFrame.append was removed; use pandas.concat")]
SIG = FailureSignature(category="removed_api", error_type="AttributeError", symbol="DataFrame.append",
                       key_line="AttributeError: 'DataFrame' object has no attribute 'append'")


def S(id_, kind, content):
    return Step(id=id_, title=id_, kind=kind, content=content)


PLAN = RunPlan(language="python", runtime="3.12", steps=[
    S("s1", "shell", "pip install pandas"),
    S("s2", "run_code", "import pandas as pd\ndf = pd.DataFrame()"),
    S("s3", "run_code", "df = df.append({'a': 1}, ignore_index=True)\nprint(len(df))"),
])


def edit(step_id, content, op="replace", ids=("r1e1",)):
    return Edit(op=op, step_id=step_id, new_content=content, reason="pandas 2.0 removed append",
                evidence_ids=list(ids))


def test_apply_edits_replace_and_insert():
    new = apply_edits(PLAN, [edit("s3", "df = pd.concat([df, pd.DataFrame([{'a': 1}])])\nprint(len(df))"),
                             Edit(op="insert_after", step_id="s1", new_content="pip install pyarrow",
                                  reason="needed", evidence_ids=["r1e1"])])
    assert [s.id for s in new.steps] == ["s1", "s1_fix1", "s2", "s3"]
    assert new.steps[3].fix_reason == "pandas 2.0 removed append" and new.steps[3].fix_evidence_ids == ["r1e1"]
    assert PLAN.steps[2].content.startswith("df = df.append")  # original untouched


def test_apply_edits_unknown_step_raises():
    import pytest
    with pytest.raises(ValueError):
        apply_edits(PLAN, [edit("nope", "x")])


def test_validate_candidate_rules():
    ids = {"r1e1"}
    ok = Candidate(strategy="migrate", rationale="r", edits=[edit("s3", "x")])
    assert validate_candidate(ok, PLAN, ids, "removed_api") is None
    uncited = Candidate(strategy="migrate", rationale="r", edits=[edit("s3", "x", ids=())])
    assert validate_candidate(uncited, PLAN, ids, "removed_api") == "edit has no citation"
    assert validate_candidate(uncited, PLAN, ids, "tutorial_typo") is None
    assert validate_candidate(uncited, PLAN, set(), "removed_api") is None  # no evidence found at all
    assert validate_candidate(Candidate(strategy="pin", rationale="r", edits=[]), PLAN, ids, "x") == "no edits"


def test_first_changed_unit():
    assert first_changed_unit(PLAN, apply_edits(PLAN, [edit("s3", "x")])) == 1
    assert first_changed_unit(PLAN, apply_edits(PLAN, [edit("s1", "pip install 'pandas<2'")])) == 0


def test_pick_prefers_migrate_then_fewer_edits():
    def rr(strategy, n, passed=True):
        return RaceResult(Candidate(strategy=strategy, rationale="", edits=[edit("s3", "x")] * n), PLAN, None,
                          passed, "")
    best = pick([rr("pin", 1), rr("migrate", 2), rr("migrate", 1), rr("migrate", 1, passed=False)])
    assert best.candidate.strategy == "migrate" and len(best.candidate.edits) == 1
    assert pick([rr("pin", 1, passed=False)]) is None


async def failing_trace(executor):
    return await executor.execute(PLAN, [ExecState(ImageRef(backend="fake", ref="base"))], run_id="r", public=True)


def bad_append(cell):
    return "df.append(" in cell["code"] or "BAD" in cell["code"]


async def test_race_runs_from_checkpoint_and_finds_winner(settings, tmp_path):
    backend = FakeBackend(cells_handler(fail_cell=bad_append))
    ex = Executor(backend, settings, Judge(None))
    trace = await failing_trace(ex)
    assert trace.failing_step_id == "s3" and trace.failed_unit == 1
    calls_before = len(backend.calls)
    good = Candidate(strategy="migrate", rationale="concat", edits=[edit("s3", "df = pd.concat([df])\nprint(1)")])
    bad = Candidate(strategy="workaround", rationale="x", edits=[edit("s3", "BAD")])
    rep = Repairer(None, ex, settings)
    results = await rep.race(plan=PLAN, trace=trace, candidates=[bad, good], failing_step_id="s3",
                             run_id="r", public=True)
    assert [r.passed for r in results] == [False, True]
    race_calls = backend.calls[calls_before:]
    assert {c["image"] for c in race_calls} == {trace.states[1].image.ref}
    assert pick(results).candidate is good


async def test_repair_step_second_round_after_failures(settings, tmp_path):
    backend = FakeBackend(cells_handler(fail_cell=bad_append))
    ex = Executor(backend, settings, Judge(None))
    trace = await failing_trace(ex)
    round1 = {"candidates": [{"strategy": "workaround", "rationale": "a", "edits": [
        {"op": "replace", "step_id": "s3", "new_content": "BAD", "reason": "r", "evidence_ids": ["r1e1"]}]}]}
    round2 = {"candidates": [{"strategy": "migrate", "rationale": "b", "edits": [
        {"op": "replace", "step_id": "s3", "new_content": "df = pd.concat([df])\nprint(1)", "reason": "r",
         "evidence_ids": ["r1e1"]}]}]}
    client = FakeChatClient([json.dumps(round1), json.dumps(round2)])
    llm = LLM(settings, Ledger(tmp_path / "l.db", settings), client=client)
    events = []

    async def emit(e):
        events.append(e)

    record, win = await Repairer(llm, ex, settings).repair_step(
        plan=PLAN, trace=trace, signature=SIG, evidence=EVID, versions="pandas==2.2.3", run_id="r", public=True,
        emit=emit)
    assert win is not None and len(record.rounds) == 2 and record.rounds[1].winner_index == 0
    assert "BAD" in client.calls[1]["messages"][1]["content"]  # previous attempt is shown to the model
    assert client.calls[0]["model"] == settings.model_repair
    assert [e["type"] for e in events].count("candidates") == 2
    assert {"type": "winner", "step_id": "s3", "round": 2, "index": 0} in events


async def test_repair_step_gives_up_after_rounds(settings, tmp_path):
    backend = FakeBackend(cells_handler(fail_cell=bad_append))
    ex = Executor(backend, settings.model_copy(update={"repair_rounds": 2}), Judge(None))
    trace = await failing_trace(ex)
    bad = json.dumps({"candidates": [{"strategy": "pin", "rationale": "a", "edits": [
        {"op": "replace", "step_id": "s3", "new_content": "BAD", "reason": "r", "evidence_ids": ["r1e1"]}]}]})
    llm = LLM(settings, Ledger(tmp_path / "l.db", settings), client=FakeChatClient([bad, bad]))

    async def emit(e):
        pass

    record, win = await Repairer(llm, ex, settings.model_copy(update={"repair_rounds": 2})).repair_step(
        plan=PLAN, trace=trace, signature=SIG, evidence=EVID, versions="", run_id="r", public=True, emit=emit)
    assert win is None and len(record.rounds) == 2


async def test_rejected_candidates_are_explained_to_the_next_round(settings, tmp_path):
    backend = FakeBackend(cells_handler(fail_cell=bad_append))
    ex = Executor(backend, settings, Judge(None))
    trace = await failing_trace(ex)
    uncited = {"candidates": [{"strategy": "migrate", "rationale": "a", "edits": [
        {"op": "replace", "step_id": "s3", "new_content": "df = pd.concat([df])\nprint(1)", "reason": "r",
         "evidence_ids": []}]}]}
    cited = {"candidates": [{"strategy": "migrate", "rationale": "b", "edits": [
        {"op": "replace", "step_id": "s3", "new_content": "df = pd.concat([df])\nprint(1)", "reason": "r",
         "evidence_ids": ["r1e1"]}]}]}
    client = FakeChatClient([json.dumps(uncited), json.dumps(cited)])
    llm = LLM(settings, Ledger(tmp_path / "l.db", settings), client=client)

    async def emit(e):
        pass

    record, win = await Repairer(llm, ex, settings).repair_step(
        plan=PLAN, trace=trace, signature=SIG, evidence=EVID, versions="", run_id="r", public=True, emit=emit)
    assert win is not None and len(record.rounds) == 2
    assert "edit has no citation" in client.calls[1]["messages"][1]["content"]


def test_validate_rejects_edits_that_change_nothing():
    same = Candidate(strategy="workaround", rationale="r", edits=[
        edit("s3", "df = df.append({'a': 1},  ignore_index=True)\n\nprint(len(df))\n")])
    assert validate_candidate(same, PLAN, {"r1e1"}, "removed_api") == "edit changes nothing"


def test_validate_rejects_edits_that_hide_failures():
    hidden = [
        edit("s1", "pip install pandas || true"),
        edit("s1", "timeout 10 pip install pandas; exit 0"),
        edit("s3", "try:\n    df = df.append({'a': 1}, ignore_index=True)\nexcept Exception:\n    pass\nprint(len(df))"),
        edit("s3", "try:\n    df = df.append({'a': 1}, ignore_index=True)\nexcept:\n    pass\nprint(len(df))"),
    ]
    for e in hidden:
        c = Candidate(strategy="workaround", rationale="r", edits=[e])
        assert validate_candidate(c, PLAN, {"r1e1"}, "removed_api") == "edit hides failures", e.new_content


def test_validate_rejects_edits_that_delete_most_of_a_step():
    long = RunPlan(language="python", runtime="3.12", steps=[S("s1", "run_code", "\n".join(
        ["import pandas as pd", "df = pd.read_csv('a.csv')", "df = df.dropna()", "df['b'] = df.a * 2",
         "df = df.append({'a': 1}, ignore_index=True)", "print(df.describe())"]))])
    gutted = Candidate(strategy="workaround", rationale="r", edits=[edit("s1", "import pandas as pd\nprint('ok')")])
    assert validate_candidate(gutted, long, {"r1e1"}, "removed_api") == "edit removes most of the step"


def test_replace_can_turn_a_blocking_command_into_a_server_step():
    plan = RunPlan(language="python", runtime="3.12", steps=[S("s1", "shell", "flask --app app run")])
    new = apply_edits(plan, [Edit(op="replace", step_id="s1", new_kind="serve", new_content="flask --app app run",
                                  reason="it starts a server", evidence_ids=[])])
    assert new.steps[0].kind == "serve" and new.steps[0].probe.port == 5000


def test_server_ports_default_to_the_framework_default():
    from fixmytutorial.planner import default_probe
    assert default_probe("flask --app app run", "python").port == 5000
    assert default_probe("streamlit run app.py", "python").port == 8501
    assert default_probe("uvicorn main:app --reload", "python").port == 8000
    assert default_probe("npm run dev", "javascript").port == 3000
    assert default_probe("python app.py --port 7000", "python").port == 7000


async def test_a_fix_passes_when_its_cell_passes_even_if_a_later_cell_breaks(settings, tmp_path):
    plan = PLAN.model_copy(update={"steps": PLAN.steps + [S("s4", "run_code", "for k, v in df.iteritems(): print(k)")]})
    backend = FakeBackend(cells_handler(fail_cell=lambda c: bad_append(c) or "iteritems" in c["code"]))
    ex = Executor(backend, settings, Judge(None))
    trace = await ex.execute(plan, [ExecState(ImageRef(backend="fake", ref="base"))], run_id="r", public=True)
    assert trace.failing_step_id == "s3"
    good = Candidate(strategy="migrate", rationale="concat", edits=[edit("s3", "df = pd.concat([df])\nprint(1)")])
    [res] = await Repairer(None, ex, settings).race(plan=plan, trace=trace, candidates=[good], failing_step_id="s3",
                                                    run_id="r", public=True)
    assert res.passed and res.trace.failing_step_id == "s4"  # the next break is left for the next repair
