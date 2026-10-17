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


async def test_batch_checks_the_sandbox_before_spending_anything(settings, monkeypatch):
    import pytest

    import fixmytutorial.rot_index as ri
    from fixmytutorial.sandbox.docker_backend import SandboxPolicyError

    class Refusing:
        name = "docker"

        async def base_image(self, language):
            raise SandboxPolicyError("the sandbox network can reach private addresses")

    started = []

    class Recorder:
        def __init__(self, **kw):
            pass

        async def run(self, rec, source):
            started.append(rec)
            return rec

    monkeypatch.setattr(ri, "get_settings", lambda: settings.model_copy(update={"sandbox_backend": "docker"}))
    monkeypatch.setattr(ri, "make_backend", lambda s: Refusing())
    monkeypatch.setattr(ri, "Pipeline", Recorder)
    monkeypatch.setattr(ri, "SearchClient", lambda *a, **k: None)
    with pytest.raises(SandboxPolicyError):
        await ri.run_batch(force=True, concurrency=1)
    assert started == []


def test_export_carries_the_breaking_release_and_publish_date(tmp_path):
    from datetime import date

    from fixmytutorial.models import BreakPoint
    st = Store(tmp_path / "s.db")
    bp = BreakPoint(package="pandas", step_id="s1", first_bad="2.0.0", first_bad_date=date(2023, 4, 3),
                    last_good="1.5.3", last_good_date=date(2023, 1, 19), how="ran")
    st.save_run(run("a", "https://a", Verdict.FIXED).model_copy(update={"broke_in": bp, "published": "2022-03-14"}))
    st.save_run(run("b", "https://b", Verdict.STILL_WORKS))
    entries = [{"url": "https://a", "title": "A", "language": "python", "source": "a.dev", "year": ""},
               {"url": "https://b", "title": "B", "language": "python", "source": "b.dev", "year": 2022}]
    out = {e["url"]: e for e in export_entries(entries, st)}
    assert out["https://a"]["broke_in"] == {"package": "pandas", "version": "2.0.0", "date": "2023-04-03", "how": "ran"}
    assert out["https://a"]["published"] == "2022-03-14" and out["https://a"]["year"] == 2022
    assert out["https://b"]["broke_in"] is None and out["https://b"]["year"] == 2022


def test_break_stats():
    from fixmytutorial.report import break_stats

    def e(pkg, ver, day, published=None):
        return {"broke_in": {"package": pkg, "version": ver, "date": day, "how": "ran"}, "published": published}

    entries = [e("pandas", "2.0.0", "2023-04-03", "2022-03-14"), e("pandas", "2.0.0", "2023-04-03", "2021-04-01"),
               e("pandas", "2.0.0", "2023-04-03", "2024-01-01"), e("numpy", "1.24.0", "2022-12-18", "2022-06-01"),
               {"broke_in": None, "published": "2020-01-01"}]
    s = break_stats(entries)
    assert s["releases"][0] == {"package": "pandas", "version": "2.0.0", "month": "Apr 2023", "count": 3}
    assert s["releases"][1]["package"] == "numpy" and s["already_out_of_date"] == 1
    assert s["median_months"] == 13  # 13, 24 and 6 months
    assert break_stats(entries[:2])["median_months"] is None  # too few to say


def test_batch_slices_rotate_through_sites_and_skip_finished_tutorials():
    # 17 Oct: every tutorial in the 2013-2018 list has its own topic, so rotating by topic was plain list order
    # (grouped by site) and a budget-cut slice would have been one blog's posts
    from fixmytutorial.rot_index import pick_entries
    entries = [{"url": f"https://{s}/{i}", "topic": f"{s} post {i}", "source": s}
               for s in ("jakevdp.github.io", "ponyfoo.com", "pbpython.com") for i in range(3)]
    picked = pick_entries(entries, done={"https://jakevdp.github.io/0"}, force=False, limit=4)
    assert [e["url"] for e in picked] == ["https://jakevdp.github.io/1", "https://ponyfoo.com/0", "https://pbpython.com/0",
                                          "https://jakevdp.github.io/2"]
    assert len(pick_entries(entries, done=set(), force=False, limit=None)) == 9
    assert pick_entries(entries, done={"https://jakevdp.github.io/0"}, force=True, limit=1)[0]["url"] == "https://jakevdp.github.io/0"


def test_named_tutorials_rerun_even_when_finished():
    from fixmytutorial.rot_index import pick_entries
    entries = [{"url": f"https://t{i}", "topic": "x"} for i in range(4)]
    picked = pick_entries(entries, done={"https://t1", "https://t2"}, force=False, limit=None,
                          only={"https://t2", "https://t3"})
    assert [e["url"] for e in picked] == ["https://t2", "https://t3"]


def test_hand_review_can_exclude_our_own_failures_and_relabel_still_works(tmp_path):
    # the review, 16 Oct: some failures were our runner's (a server step, a misread command), not the tutorial's
    st = Store(tmp_path / "s.db")
    st.save_run(run("x", "https://x", Verdict.COULD_NOT_FIX))
    st.save_run(run("y", "https://y", Verdict.FIXED))
    entries = [
        {"url": "https://x", "title": "X", "language": "python", "source": "x.dev", "year": 2015, "review": "exclude"},
        {"url": "https://y", "title": "Y", "language": "python", "source": "y.dev", "year": 2016, "review": "still_works"},
    ]
    out = {e["url"]: e for e in export_entries(entries, st)}
    assert set(out) == {"https://y"}
    assert out["https://y"]["verdict"] == "still_works" and out["https://y"]["changes"] == 0
    assert out["https://y"]["categories"] == []


def test_hand_review_corrects_causes_and_break_dates_and_unlinks_overturned_reports(tmp_path):
    # the review, 17 Oct: Python 2 prints were filed as tutorial typos, real breaks went undated, and a report saying
    # "Fixed" next to a hand-downgraded "Could not fix" would contradict the index
    from datetime import date
    st = Store(tmp_path / "s.db")
    st.save_run(run("k", "https://k", Verdict.COULD_NOT_FIX))
    st.save_run(run("d", "https://d", Verdict.FIXED, category="tutorial_typo"))
    st.save_run(run("s", "https://s", Verdict.COULD_NOT_FIX))
    entries = [
        {"url": "https://k", "title": "K", "language": "python", "source": "k.dev", "year": 2013,
         "categories": ["removed_api"], "broke_in": {"package": "matplotlib", "version": "3.5.0",
                                                     "date": date(2021, 11, 16), "how": "clear_temp was removed"}},
        {"url": "https://d", "title": "D", "language": "python", "source": "d.dev", "year": 2014, "review": "downgrade",
         "categories": ["removed_api"]},
        {"url": "https://s", "title": "S", "language": "python", "source": "s.dev", "year": 2016, "review": "still_works"},
    ]
    out = {e["url"]: e for e in export_entries(entries, st)}
    assert out["https://k"]["categories"] == ["removed_api"] and out["https://k"]["run_id"] == "k"
    assert out["https://k"]["broke_in"] == {"package": "matplotlib", "version": "3.5.0", "date": "2021-11-16",
                                            "how": "clear_temp was removed"}
    assert out["https://d"]["verdict"] == "could_not_fix" and out["https://d"]["categories"] == ["removed_api"]
    assert out["https://d"]["run_id"] is None and out["https://d"]["changes"] == 0
    assert out["https://s"]["run_id"] is None
