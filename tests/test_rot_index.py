from fixmytutorial.models import (
    Candidate, Edit, FailureSignature, RepairRecord, RunPlan, RunRecord, RunStatus, Step, Verdict, utcnow,
)
from fixmytutorial.rot_index import export_entries
from fixmytutorial.store import Store


def run(id_, url, verdict, public=False, category="removed_api"):
    plan = RunPlan(language="python", runtime="3.12", steps=[Step(id="s1", title="t", kind="run_code", content="a")])
    final = RunPlan(language="python", runtime="3.12",
                    steps=[plan.steps[0].model_copy(update={"content": "b"})]) if verdict == Verdict.FIXED else plan
    repairs = [RepairRecord(step_id="s1", signature=FailureSignature(category=category),
                            winner=Candidate(strategy="migrate", rationale="", edits=[
                                Edit(step_id="s1", new_content="b", reason="r")]))] if verdict == Verdict.FIXED else []
    return RunRecord(id=id_, source=url, public=public, status=RunStatus.DONE, verdict=verdict, plan=plan,
                     final_plan=final, repairs=repairs, created_at=utcnow())


def test_export_entries(tmp_path):
    st = Store(tmp_path / "s.db")
    st.save_run(run("a", "https://a", Verdict.FIXED))
    st.save_run(run("b", "https://b", Verdict.STILL_WORKS))
    st.save_run(run("c", "https://c", Verdict.ERROR))
    st.save_run(run("d", "https://d", Verdict.FIXED))
    st.save_run(run("pub", "https://e", Verdict.FIXED, public=True))
    entries = [
        {"url": "https://a", "title": "A", "language": "python", "source": "a.dev", "year": 2021, "featured": True},
        {"url": "https://b", "title": "B", "language": "python", "source": "b.dev", "year": 2022},
        {"url": "https://c", "title": "C", "language": "python", "source": "c.dev", "year": 2022},
        {"url": "https://d", "title": "D", "language": "python", "source": "d.dev", "year": 2020, "review": "downgrade"},
        {"url": "https://e", "title": "E", "language": "python", "source": "e.dev", "year": 2020},
    ]
    out = {e["url"]: e for e in export_entries(entries, st)}
    assert set(out) == {"https://a", "https://b", "https://d"}  # errors and public-only runs are excluded
    assert out["https://a"]["verdict"] == "fixed" and out["https://a"]["categories"] == ["removed_api"]
    assert out["https://a"]["changes"] == 1 and out["https://a"]["featured"] is True and out["https://a"]["run_id"] == "a"
    assert out["https://d"]["verdict"] == "could_not_fix"  # downgraded after hand review


def test_batch_refuses_third_party_code_on_docker_with_private_egress(settings):
    import pytest

    from fixmytutorial.rot_index import check_batch_backend
    with pytest.raises(SystemExit):
        check_batch_backend(settings.model_copy(update={"sandbox_backend": "docker",
                                                        "docker_allow_private_egress": True}))
    check_batch_backend(settings.model_copy(update={"sandbox_backend": "contree"}))
    check_batch_backend(settings.model_copy(update={"sandbox_backend": "docker"}))  # probe still guards it
