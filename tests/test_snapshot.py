import json
import shutil
from pathlib import Path

import pytest

from fixmytutorial.ledger import Ledger
from fixmytutorial.models import RunRecord, RunStatus, Verdict, utcnow
from fixmytutorial.snapshot import Snapshotter
from fixmytutorial.store import Store


class FakeHub:
    """Stores uploads in a local folder that snapshot_download returns."""

    def __init__(self, root: Path):
        self.root = root
        self.commits = 0

    def create_repo(self, **kw):
        self.root.mkdir(parents=True, exist_ok=True)

    def upload_file(self, *, path_or_fileobj, path_in_repo, **kw):
        p = self.root / path_in_repo
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(path_or_fileobj)
        self.commits += 1

    def upload_folder(self, *, folder_path, **kw):
        shutil.copytree(folder_path, self.root, dirs_exist_ok=True)
        self.commits += 1

    def snapshot_download(self, **kw):
        return str(self.root)


def deps(settings, tmp, name):
    d = tmp / name
    d.mkdir()
    return Store(d / "s.db"), Ledger(d / "l.db", settings)


def rec(i):
    return RunRecord(id=f"r{i}", source="https://x", status=RunStatus.DONE, verdict=Verdict.FIXED, created_at=utcnow())


def test_save_and_restore_round_trip(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    store, ledger = deps(settings, tmp_path, "a")
    store.save_run(rec(1))
    store.append_event("r1", {"type": "stage"})
    res = ledger.reserve_llm("r1", True, "planner", 1000, 100)
    ledger.commit_llm(res, "planner", "m", 1000, 100)
    snap = Snapshotter(settings, store, ledger, api=hub, owns_ledger=True)
    assert snap.save_all([store.get_run("r1")], bootstrap=True)
    store2, ledger2 = deps(settings, tmp_path, "b")
    assert Snapshotter(settings, store2, ledger2, api=hub, owns_ledger=True).restore() == 1
    assert store2.get_run("r1").verdict == Verdict.FIXED
    assert [e["type"] for _, e in store2.events_after("r1", 0)] == ["stage"]
    assert ledger2.totals().lifetime_usd == ledger.totals().lifetime_usd


def test_save_all_is_one_commit(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    store, ledger = deps(settings, tmp_path, "a")
    for i in range(5):
        store.save_run(rec(i))
    Snapshotter(settings, store, ledger, api=hub).save_all(store.list_runs(), bootstrap=True)
    assert hub.commits == 1
    assert len(list((tmp_path / "hub" / "runs").glob("*.json"))) == 5


def test_disabled_without_token(settings, tmp_path):
    store, ledger = deps(settings, tmp_path, "a")
    snap = Snapshotter(settings.model_copy(update={"hf_token": ""}), store, ledger)
    assert not snap.enabled and snap.restore() == 0
    snap.save_run(rec(1))  # no error


def test_network_errors_are_swallowed(settings, tmp_path):
    class Broken(FakeHub):
        def upload_file(self, **kw):
            raise OSError("network down")
    store, ledger = deps(settings, tmp_path, "a")
    Snapshotter(settings, store, ledger, api=Broken(tmp_path / "hub")).save_run(rec(1))


def test_restore_failure_is_reported(settings, tmp_path):
    class Offline(FakeHub):
        def snapshot_download(self, **kw):
            raise OSError("hub unreachable")
    store, ledger = deps(settings, tmp_path, "a")
    snap = Snapshotter(settings, store, ledger, api=Offline(tmp_path / "hub"), owns_ledger=True)
    assert snap.restore() == 0 and snap.restore_ok is False


def test_missing_ledger_file_fails_closed(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    (tmp_path / "hub" / "runs").mkdir(parents=True)
    (tmp_path / "hub" / "runs" / "r1.json").write_text(rec(1).model_dump_json())
    store, ledger = deps(settings, tmp_path, "a")
    snap = Snapshotter(settings, store, ledger, api=hub, owns_ledger=True)
    snap.restore()
    assert snap.restore_ok is False and not ledger.public_runs_open()[0]


def test_corrupt_ledger_file_fails_closed(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    (tmp_path / "hub").mkdir(parents=True)
    (tmp_path / "hub" / "ledger.json").write_text("{not json")
    store, ledger = deps(settings, tmp_path, "a")
    snap = Snapshotter(settings, store, ledger, api=hub, owns_ledger=True)
    snap.restore()
    assert snap.restore_ok is False and not ledger.public_runs_open()[0]


def test_failed_restore_pauses_public_runs_and_never_overwrites_remote_ledger(settings, tmp_path):
    class Offline(FakeHub):
        def snapshot_download(self, **kw):
            raise OSError("hub unreachable")
    hub = Offline(tmp_path / "hub")
    store, ledger = deps(settings, tmp_path, "a")
    snap = Snapshotter(settings, store, ledger, api=hub, owns_ledger=True)
    snap.restore()
    assert not ledger.public_runs_open()[0]
    snap.save_run(rec(1))
    assert not (tmp_path / "hub" / "ledger.json").exists()


def test_save_failure_pauses_public_runs_until_next_success(settings, tmp_path):
    class Flaky(FakeHub):
        fail = True

        def upload_file(self, **kw):
            if self.fail:
                raise OSError("network down")
            return super().upload_file(**kw)
    hub = Flaky(tmp_path / "hub")
    store, ledger = deps(settings, tmp_path, "a")
    snap = Snapshotter(settings, store, ledger, api=hub, owns_ledger=True)
    snap.save_run(rec(1))
    assert not ledger.public_runs_open()[0]
    hub.fail = False
    snap.save_run(rec(1))
    assert ledger.public_runs_open()[0]


def test_save_all_refuses_to_overwrite_when_remote_is_unreadable(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    (tmp_path / "hub").mkdir(parents=True)
    (tmp_path / "hub" / "ledger.json").write_text("{not json")
    store, ledger = deps(settings, tmp_path, "a")
    assert Snapshotter(settings, store, ledger, api=hub, owns_ledger=True).save_all([rec(1)], bootstrap=True) is False
    assert (tmp_path / "hub" / "ledger.json").read_text() == "{not json"


def space(settings, store, ledger, hub):
    return Snapshotter(settings, store, ledger, api=hub, owns_ledger=True)


def spend(ledger, run_id, tokens):
    r = ledger.reserve_llm(run_id, True, "planner", tokens, 100)
    ledger.commit_llm(r, "planner", "m", tokens, 100)


def test_laptop_sync_never_writes_the_remote_ledger(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    prod_store, prod_ledger = deps(settings, tmp_path, "prod")
    spend(prod_ledger, "pub1", 1000)
    space(settings, prod_store, prod_ledger, hub).save_all([], bootstrap=True)
    local_store, local_ledger = deps(settings, tmp_path, "local")
    spend(local_ledger, "batch1", 2000)
    local_store.save_run(rec(7))
    assert Snapshotter(settings, local_store, local_ledger, api=hub).save_all(local_store.list_runs())
    remote = json.loads((tmp_path / "hub" / "ledger.json").read_text())
    assert {row["run_id"] for row in remote} == {"pub1"}
    assert (tmp_path / "hub" / "runs" / "r7.json").exists()


def test_space_spend_survives_laptop_syncs_and_restarts(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    a_store, a_ledger = deps(settings, tmp_path, "space1")
    space(settings, a_store, a_ledger, hub).save_all([], bootstrap=True)
    spend(a_ledger, "pub1", 5000)
    a = space(settings, a_store, a_ledger, hub)
    a.restore()
    l_store, l_ledger = deps(settings, tmp_path, "laptop")
    spend(l_ledger, "batch1", 8000)
    Snapshotter(settings, l_store, l_ledger, api=hub).save_all([])
    spend(a_ledger, "pub2", 1000)
    a_store.save_run(rec(1))
    a.save_run(a_store.get_run("r1"))
    b_store, b_ledger = deps(settings, tmp_path, "space2")
    space(settings, b_store, b_ledger, hub).restore()
    assert b_ledger.totals().lifetime_usd == pytest.approx(a_ledger.totals().lifetime_usd)
    assert {row["run_id"] for row in b_ledger.export_rows()} == {"pub1", "pub2"}


def test_laptop_bootstrap_creates_an_empty_ledger_for_the_first_space_boot(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    l_store, l_ledger = deps(settings, tmp_path, "laptop")
    spend(l_ledger, "batch1", 1000)
    assert Snapshotter(settings, l_store, l_ledger, api=hub).save_all([], bootstrap=True)
    assert json.loads((tmp_path / "hub" / "ledger.json").read_text()) == []
    s_store, s_ledger = deps(settings, tmp_path, "space")
    snap = space(settings, s_store, s_ledger, hub)
    snap.restore()
    assert snap.restore_ok and s_ledger.public_runs_open()[0]


def test_laptop_restore_ignores_the_remote_ledger(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    (tmp_path / "hub" / "runs").mkdir(parents=True)
    (tmp_path / "hub" / "runs" / "r1.json").write_text(rec(1).model_dump_json())
    l_store, l_ledger = deps(settings, tmp_path, "laptop")
    assert Snapshotter(settings, l_store, l_ledger, api=hub).restore() == 1
    assert l_ledger.public_runs_open()[0] and l_ledger.export_rows() == []


def test_concurrent_saves_upload_the_newest_ledger_last(settings, tmp_path):
    import threading

    class Slow(FakeHub):
        """The first ledger upload stalls until a second save has started."""
        def __init__(self, root):
            super().__init__(root)
            self.first_ledger = threading.Event()
            self.second_started = threading.Event()

        def upload_file(self, *, path_or_fileobj, path_in_repo, **kw):
            if path_in_repo == "ledger.json" and not self.first_ledger.is_set():
                self.first_ledger.set()
                self.second_started.wait(timeout=2)
            return super().upload_file(path_or_fileobj=path_or_fileobj, path_in_repo=path_in_repo, **kw)

    hub = Slow(tmp_path / "hub")
    store, ledger = deps(settings, tmp_path, "space")
    space(settings, store, ledger, hub).save_all([], bootstrap=True)
    hub.first_ledger.clear()
    snap = space(settings, store, ledger, hub)
    snap.restore()
    spend(ledger, "pub1", 1000)
    store.save_run(rec(1))
    t1 = threading.Thread(target=snap.save_run, args=(store.get_run("r1"),))
    t1.start()
    hub.first_ledger.wait(timeout=1)
    spend(ledger, "pub2", 1000)
    store.save_run(rec(2))
    t2 = threading.Thread(target=snap.save_run, args=(store.get_run("r2"),))
    t2.start()
    t2.join(timeout=0.5)  # without a lock the second save finishes here, before the stalled first one
    hub.second_started.set()
    t1.join()
    t2.join()
    remote = json.loads((tmp_path / "hub" / "ledger.json").read_text())
    assert {row["run_id"] for row in remote} == {"pub1", "pub2"}


def test_one_bad_run_file_does_not_block_restore(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    (tmp_path / "hub" / "runs").mkdir(parents=True)
    (tmp_path / "hub" / "ledger.json").write_text("[]")
    (tmp_path / "hub" / "runs" / "bad.json").write_text("{not json")
    (tmp_path / "hub" / "runs" / "r1.json").write_text(rec(1).model_dump_json())
    store, ledger = deps(settings, tmp_path, "space")
    snap = space(settings, store, ledger, hub)
    assert snap.restore() == 1 and snap.restore_ok


def test_heal_retries_a_failed_restore(settings, tmp_path):
    class Flaky(FakeHub):
        down = True

        def snapshot_download(self, **kw):
            if self.down:
                raise OSError("hub unreachable")
            return super().snapshot_download(**kw)
    hub = Flaky(tmp_path / "hub")
    (tmp_path / "hub").mkdir(parents=True)
    (tmp_path / "hub" / "ledger.json").write_text("[]")
    store, ledger = deps(settings, tmp_path, "space")
    snap = space(settings, store, ledger, hub)
    snap.restore()
    assert not ledger.public_runs_open()[0]
    hub.down = False
    snap.heal()
    assert ledger.public_runs_open()[0]


def test_heal_retries_a_failed_ledger_save(settings, tmp_path):
    class Flaky(FakeHub):
        fail = False

        def upload_file(self, **kw):
            if self.fail:
                raise OSError("network down")
            return super().upload_file(**kw)
    hub = Flaky(tmp_path / "hub")
    store, ledger = deps(settings, tmp_path, "space")
    snap = space(settings, store, ledger, hub)
    snap.save_all([], bootstrap=True)
    snap.restore()
    spend(ledger, "pub1", 1000)
    store.save_run(rec(1))
    hub.fail = True
    snap.save_run(store.get_run("r1"))
    assert not ledger.public_runs_open()[0]
    hub.fail = False
    snap.heal()
    assert ledger.public_runs_open()[0]
    assert {row["run_id"] for row in json.loads((tmp_path / "hub" / "ledger.json").read_text())} == {"pub1"}


def test_bootstrap_never_recreates_a_ledger_for_a_dataset_that_has_runs(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    (tmp_path / "hub" / "runs").mkdir(parents=True)
    (tmp_path / "hub" / "runs" / "r1.json").write_text(rec(1).model_dump_json())
    l_store, l_ledger = deps(settings, tmp_path, "laptop")
    assert Snapshotter(settings, l_store, l_ledger, api=hub).save_all([], bootstrap=True) is False
    assert not (tmp_path / "hub" / "ledger.json").exists()


def test_restore_passes_each_run_its_own_time(tmp_path):
    import json as _json
    from datetime import datetime, timezone

    from fixmytutorial.snapshot import Snapshotter
    calls = []

    class Store:
        def get_run(self, rid):
            return None

        def save_run(self, rec, at=None):
            calls.append((rec.id, at))

        def events_after(self, rid, seq):
            return []

    (tmp_path / "runs").mkdir()
    done = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
    (tmp_path / "runs" / "a.json").write_text(_json.dumps({"id": "a", "source": "https://a", "created_at": done.isoformat(),
                                                          "finished_at": done.isoformat()}))
    snap = Snapshotter.__new__(Snapshotter)
    snap.store = Store()
    assert snap._restore_runs(tmp_path) == 1 and calls == [("a", done.timestamp())]
