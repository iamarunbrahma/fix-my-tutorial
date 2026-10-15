import json
import shutil
from pathlib import Path

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
    snap = Snapshotter(settings, store, ledger, api=hub)
    assert snap.save_all([store.get_run("r1")], bootstrap=True)
    store2, ledger2 = deps(settings, tmp_path, "b")
    assert Snapshotter(settings, store2, ledger2, api=hub).restore() == 1
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
    snap = Snapshotter(settings, store, ledger, api=Offline(tmp_path / "hub"))
    assert snap.restore() == 0 and snap.restore_ok is False


def test_missing_ledger_file_fails_closed(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    (tmp_path / "hub" / "runs").mkdir(parents=True)
    (tmp_path / "hub" / "runs" / "r1.json").write_text(rec(1).model_dump_json())
    store, ledger = deps(settings, tmp_path, "a")
    snap = Snapshotter(settings, store, ledger, api=hub)
    snap.restore()
    assert snap.restore_ok is False and not ledger.public_runs_open()[0]


def test_corrupt_ledger_file_fails_closed(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    (tmp_path / "hub").mkdir(parents=True)
    (tmp_path / "hub" / "ledger.json").write_text("{not json")
    store, ledger = deps(settings, tmp_path, "a")
    snap = Snapshotter(settings, store, ledger, api=hub)
    snap.restore()
    assert snap.restore_ok is False and not ledger.public_runs_open()[0]


def test_failed_restore_pauses_public_runs_and_never_overwrites_remote_ledger(settings, tmp_path):
    class Offline(FakeHub):
        def snapshot_download(self, **kw):
            raise OSError("hub unreachable")
    hub = Offline(tmp_path / "hub")
    store, ledger = deps(settings, tmp_path, "a")
    snap = Snapshotter(settings, store, ledger, api=hub)
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
    snap = Snapshotter(settings, store, ledger, api=hub)
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
    assert Snapshotter(settings, store, ledger, api=hub).save_all([rec(1)], bootstrap=True) is False
    assert (tmp_path / "hub" / "ledger.json").read_text() == "{not json"


def test_save_all_merges_remote_ledger_rows_before_upload(settings, tmp_path):
    hub = FakeHub(tmp_path / "hub")
    prod_store, prod_ledger = deps(settings, tmp_path, "prod")
    r = prod_ledger.reserve_llm("pub1", True, "planner", 1000, 100)
    prod_ledger.commit_llm(r, "planner", "m", 1000, 100)
    Snapshotter(settings, prod_store, prod_ledger, api=hub).save_all([], bootstrap=True)
    local_store, local_ledger = deps(settings, tmp_path, "local")
    r2 = local_ledger.reserve_llm("batch1", False, "planner", 2000, 100)
    local_ledger.commit_llm(r2, "planner", "m", 2000, 100)
    assert Snapshotter(settings, local_store, local_ledger, api=hub).save_all([])
    remote = json.loads((tmp_path / "hub" / "ledger.json").read_text())
    assert {row["run_id"] for row in remote} == {"pub1", "batch1"}
