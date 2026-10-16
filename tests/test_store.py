from fixmytutorial.models import RunRecord, RunStatus, Verdict, utcnow
from fixmytutorial.store import Store


def rec(id_, source="https://a", status=RunStatus.DONE, verdict=Verdict.FIXED):
    return RunRecord(id=id_, source=source, status=status, verdict=verdict, created_at=utcnow())


def test_save_and_get_run(tmp_path):
    st = Store(tmp_path / "s.db")
    st.save_run(rec("r1"))
    got = st.get_run("r1")
    assert got.id == "r1" and got.verdict == Verdict.FIXED
    assert st.get_run("missing") is None


def test_save_run_upserts(tmp_path):
    st = Store(tmp_path / "s.db")
    r = rec("r1", status=RunStatus.EXECUTING, verdict=None)
    st.save_run(r)
    r.status, r.verdict = RunStatus.DONE, Verdict.STILL_WORKS
    st.save_run(r)
    assert st.get_run("r1").verdict == Verdict.STILL_WORKS
    assert len(st.list_runs()) == 1


def test_find_recent_run_skips_errors_and_old(tmp_path):
    t = {"now": 1000.0}
    st = Store(tmp_path / "s.db", clock=lambda: t["now"])
    st.save_run(rec("old", verdict=Verdict.FIXED))
    t["now"] += 90_000
    st.save_run(rec("err", verdict=Verdict.ERROR))
    st.save_run(rec("running", status=RunStatus.EXECUTING, verdict=None))
    assert st.find_recent_run("https://a", within_s=86_400) is None
    st.save_run(rec("good", verdict=Verdict.STILL_WORKS))
    assert st.find_recent_run("https://a", within_s=86_400).id == "good"


def test_events_are_ordered_and_incremental(tmp_path):
    st = Store(tmp_path / "s.db")
    assert st.append_event("r1", {"type": "a"}) == 1
    assert st.append_event("r1", {"type": "b"}) == 2
    st.append_event("r2", {"type": "x"})
    assert [e["type"] for _, e in st.events_after("r1", 0)] == ["a", "b"]
    assert [s for s, _ in st.events_after("r1", 1)] == [2]


def test_cache_expiry(tmp_path):
    t = {"now": 0.0}
    st = Store(tmp_path / "s.db", clock=lambda: t["now"])
    st.cache_put("k", {"v": [1, 2]}, ttl_s=10)
    assert st.cache_get("k") == {"v": [1, 2]}
    t["now"] = 11
    assert st.cache_get("k") is None


def test_restored_runs_keep_their_own_finish_time(tmp_path):
    from datetime import datetime, timezone
    t = {"now": datetime(2026, 10, 16, 12, tzinfo=timezone.utc).timestamp()}
    st = Store(tmp_path / "s.db", clock=lambda: t["now"])
    newer = rec("newer").model_copy(update={"finished_at": datetime(2026, 10, 16, 9, tzinfo=timezone.utc)})
    older = rec("older").model_copy(update={"finished_at": datetime(2026, 10, 16, 8, tzinfo=timezone.utc)})
    stale = rec("stale", source="https://b").model_copy(update={"finished_at": datetime(2026, 10, 13, tzinfo=timezone.utc)})
    for r in (newer, older, stale):  # restore order must not decide which report is "most recent"
        st.save_run(r, at=r.finished_at.timestamp())
    assert st.find_recent_run("https://a", within_s=86_400).id == "newer"
    assert st.find_recent_run("https://b", within_s=86_400) is None  # three days old is not recent
