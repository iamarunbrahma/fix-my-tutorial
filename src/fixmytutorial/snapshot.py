"""Persist finished runs, their events and the Space's spend ledger to a private Hugging Face dataset.

Free Spaces have no persistent disk. The ledger is what keeps the Space's spend cap true across restarts, so the
Space is its only writer (owns_ledger): the laptop keeps its own ledger and budget, and its `rot_index sync` uploads
runs and events but never ledger.json, so it can never erase spend the Space recorded. Every Space path fails
closed: if past spend cannot be loaded or saved, public runs pause until heal() succeeds.
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
from pathlib import Path

from .models import RunRecord

log = logging.getLogger("fixmytutorial.snapshot")
RESTORE_FAILED = "past spend could not be loaded."
SAVE_FAILED = "spend records could not be saved."
NOT_CONFIGURED = "spend records are not set up."


class HubAdapter:
    def __init__(self, token: str):
        from huggingface_hub import HfApi
        self._api = HfApi(token=token)
        self._token = token

    def create_repo(self, **kw):
        return self._api.create_repo(**kw)

    def upload_file(self, **kw):
        return self._api.upload_file(**kw)

    def upload_folder(self, **kw):
        return self._api.upload_folder(**kw)

    def snapshot_download(self, **kw) -> str:
        from huggingface_hub import snapshot_download
        return snapshot_download(token=self._token, **kw)


class Snapshotter:
    def __init__(self, settings, store, ledger, api=None, owns_ledger: bool | None = None):
        self.repo = settings.hf_dataset_repo
        self.store = store
        self.ledger = ledger
        self.api = api if api is not None else (HubAdapter(settings.hf_token) if settings.hf_token else None)
        self.enabled = self.api is not None
        self.owns_ledger = bool(settings.space_id) if owns_ledger is None else owns_ledger
        self.restore_ok = True  # False after a restore that could not load the remote ledger
        self.last_restore_error = ""  # "", "unreachable", "missing" or "corrupt"
        self._merged = False  # True once this process has loaded the remote ledger (safe to upload ours)
        self._repo_ready = False
        self._lock = threading.Lock()  # one save at a time, so the newest ledger is always the last one uploaded

    def _ensure_repo(self) -> None:
        if not self._repo_ready:
            self.api.create_repo(repo_id=self.repo, repo_type="dataset", private=True, exist_ok=True)
            self._repo_ready = True

    def _upload(self, data: bytes, path: str) -> None:
        self.api.upload_file(path_or_fileobj=data, path_in_repo=path, repo_id=self.repo, repo_type="dataset",
                             commit_message=f"Update {path}")

    def _download(self, patterns: list[str]) -> Path:
        self._ensure_repo()
        return Path(self.api.snapshot_download(repo_id=self.repo, repo_type="dataset", allow_patterns=patterns))

    def _restore_runs(self, local: Path) -> int:
        restored = 0
        for f in sorted(local.glob("runs/*.json")):
            try:  # one unreadable file (old schema, corrupt upload) must not take the whole site down
                rec = RunRecord.model_validate_json(f.read_text())
                if self.store.get_run(rec.id) is None:
                    self.store.save_run(rec)
                    restored += 1
                ev_file = local / "events" / f"{rec.id}.json"
                if ev_file.exists() and not self.store.events_after(rec.id, 0):
                    for ev in json.loads(ev_file.read_text()):
                        self.store.append_event(rec.id, ev)
            except (ValueError, OSError):
                log.exception("skipping unreadable snapshot file %s", f.name)
        return restored

    def restore(self) -> int:
        if not self.enabled:
            return 0
        patterns = ["runs/*.json", "events/*.json"] + (["ledger.json"] if self.owns_ledger else [])
        try:
            local = self._download(patterns)
        except Exception:
            log.exception("snapshot restore failed")
            return self._restore_failed("unreachable") if self.owns_ledger else 0
        if not self.owns_ledger:
            return self._restore_runs(local)
        ledger_file = local / "ledger.json"
        if not ledger_file.exists():
            return self._restore_failed("missing")
        try:
            rows = json.loads(ledger_file.read_text())
            if not isinstance(rows, list):
                raise ValueError("ledger.json is not a list")
            self.ledger.import_rows(rows)  # before the runs, so a bad run file can never skip it
        except (ValueError, KeyError, TypeError):
            log.exception("ledger.json is corrupt")
            return self._restore_failed("corrupt")
        self.restore_ok, self.last_restore_error, self._merged = True, "", True
        self.ledger.clear_unverified(RESTORE_FAILED)
        return self._restore_runs(local)

    def _restore_failed(self, why: str) -> int:
        self.restore_ok, self.last_restore_error = False, why
        self.ledger.mark_unverified(RESTORE_FAILED)
        return 0

    def save_run(self, rec: RunRecord) -> None:
        if not self.enabled:
            return
        with self._lock:
            try:
                self._ensure_repo()
                self._upload(rec.model_dump_json().encode(), f"runs/{rec.id}.json")
                events = [ev for _, ev in self.store.events_after(rec.id, 0)]
                self._upload(json.dumps(events).encode(), f"events/{rec.id}.json")
                if self.owns_ledger and self._merged:
                    self._upload(json.dumps(self.ledger.export_rows()).encode(), "ledger.json")
            except Exception:
                log.exception("snapshot save failed for %s", rec.id)
                if self.owns_ledger:
                    self.ledger.mark_unverified(SAVE_FAILED)
                return
        if self.owns_ledger:
            self.ledger.clear_unverified(SAVE_FAILED)

    def save_ledger(self) -> bool:
        if not (self.enabled and self.owns_ledger and self._merged):
            return False
        with self._lock:
            try:
                self._ensure_repo()
                self._upload(json.dumps(self.ledger.export_rows()).encode(), "ledger.json")
            except Exception:
                log.exception("ledger save failed")
                self.ledger.mark_unverified(SAVE_FAILED)
                return False
        self.ledger.clear_unverified(SAVE_FAILED)
        return True

    def heal(self) -> None:
        """Retry whatever paused public runs. Called periodically by the web app while they are paused."""
        if not (self.enabled and self.owns_ledger):
            return
        reasons = self.ledger.unverified()
        if RESTORE_FAILED in reasons:
            self.restore()
        elif SAVE_FAILED in reasons:
            self.save_ledger()

    def _remote_ledger_exists(self) -> bool | None:
        try:
            return (self._download(["ledger.json"]) / "ledger.json").exists()
        except Exception:
            log.exception("could not check the remote ledger")
            return None

    def save_all(self, records: list[RunRecord], bootstrap: bool = False) -> bool:
        """Upload runs and events in one commit. The Space also uploads its ledger, after loading the remote one
        (refusing if that fails, unless nothing is there yet). The laptop never uploads ledger rows; on bootstrap it
        creates an empty ledger.json so the Space's first boot finds one."""
        if not self.enabled:
            return False
        ledger_rows: list[dict] | None = None
        if self.owns_ledger:
            self.restore()
            if not self.restore_ok and not (bootstrap and self.last_restore_error == "missing"):
                log.error("refusing to upload: remote ledger is %s", self.last_restore_error)
                return False
        elif bootstrap:
            exists = self._remote_ledger_exists()
            if exists is None:
                return False
            if not exists:
                ledger_rows = []
        with self._lock:
            try:
                self._ensure_repo()
                with tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    (root / "runs").mkdir()
                    (root / "events").mkdir()
                    for rec in records:
                        (root / "runs" / f"{rec.id}.json").write_text(rec.model_dump_json())
                        events = [ev for _, ev in self.store.events_after(rec.id, 0)]
                        (root / "events" / f"{rec.id}.json").write_text(json.dumps(events))
                    if self.owns_ledger:
                        ledger_rows = self.ledger.export_rows()
                    if ledger_rows is not None:
                        (root / "ledger.json").write_text(json.dumps(ledger_rows))
                    self.api.upload_folder(folder_path=d, repo_id=self.repo, repo_type="dataset",
                                           commit_message=f"Batch update ({len(records)} runs)")
            except Exception:
                log.exception("snapshot batch save failed")
                return False
        if self.owns_ledger:
            self._merged, self.restore_ok, self.last_restore_error = True, True, ""
            self.ledger.clear_unverified(RESTORE_FAILED)
        return True
