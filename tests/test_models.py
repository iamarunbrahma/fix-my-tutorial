import json

from fixmytutorial.models import (
    Candidate, Edit, PlannerOutput, RunPlan, RunRecord, Step, StepOutcome, StepStatus, Verdict, utcnow,
)


def test_planner_output_schema_has_no_fix_fields():
    schema = json.dumps(PlannerOutput.model_json_schema())
    assert "fix_reason" not in schema and "fix_evidence_ids" not in schema


def test_step_defaults():
    s = Step(id="s1", title="Install", kind="shell", content="pip install pandas")
    assert s.expect.exit_code == 0 and s.expect.stdout_contains == []
    assert s.fix_reason == "" and s.fix_evidence_ids == []


def test_run_record_round_trips_json():
    plan = RunPlan(language="python", runtime="3.12",
                   steps=[Step(id="s1", title="t", kind="run_code", content="print(1)")])
    rec = RunRecord(id="r1", source="https://x", created_at=utcnow(), plan=plan)
    rec.outcomes["s1"] = StepOutcome(step_id="s1", status=StepStatus.PASSED)
    again = RunRecord.model_validate_json(rec.model_dump_json())
    assert again.plan.steps[0].content == "print(1)"
    assert again.outcomes["s1"].status == StepStatus.PASSED


def test_candidate_requires_strategy():
    c = Candidate(strategy="migrate", rationale="r",
                  edits=[Edit(step_id="s1", new_content="x", reason="because")])
    assert c.edits[0].op == "replace"


def test_verdict_values_are_stable_strings():
    assert {v.value for v in Verdict} == {"still_works", "fixed", "could_not_fix", "not_runnable", "error"}
