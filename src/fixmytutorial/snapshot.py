"""Persist finished runs, their events and the spend ledger to a private Hugging Face dataset.

Free Spaces have no persistent disk. Restoring the ledger on boot is what keeps the spend caps true across restarts.
"""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from .models import RunRecord

log = logging.getLogger("fixmytutorial.snapshot")


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
    def __init__(self, settings, store, ledger, api=None):
        self.repo = settings.hf_dataset_repo
        self.store = store
        self.ledger = ledger
        self.api = api if api is not None else (HubAdapter(settings.hf_token) if settings.hf_token else None)
        self.enabled = self.api is not None
        self.restore_ok = True  # False when the last restore could not reach the dataset
        self._repo_ready = False

    def _ensure_repo(self) -> None:
        if not self._repo_ready:
            self.api.create_repo(repo_id=self.repo, repo_type="dataset", private=True, exist_ok=True)
            self._repo_ready = True

    def _upload(self, data: bytes, path: str) -> None:
        self.api.upload_file(path_or_fileobj=data, path_in_repo=path, repo_id=self.repo, repo_type="dataset",
                             commit_message=f"Update {path}")

    def save_run(self, rec: RunRecord) -> None:
        if not self.enabled:
            return
        try:
            self._ensure_repo()
            self._upload(rec.model_dump_json().encode(), f"runs/{rec.id}.json")
            events = [ev for _, ev in self.store.events_after(rec.id, 0)]
            self._upload(json.dumps(events).encode(), f"events/{rec.id}.json")
            self._upload(json.dumps(self.ledger.export_rows()).encode(), "ledger.json")
        except Exception:
            log.exception("snapshot save failed for %s", rec.id)

    def save_all(self, records: list[RunRecord]) -> None:
        if not self.enabled:
            return
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
                (root / "ledger.json").write_text(json.dumps(self.ledger.export_rows()))
                self.api.upload_folder(folder_path=d, repo_id=self.repo, repo_type="dataset",
                                       commit_message=f"Batch update ({len(records)} runs)")
        except Exception:
            log.exception("snapshot batch save failed")

    def restore(self) -> int:
        if not self.enabled:
            return 0
        try:
            self._ensure_repo()
            local = Path(self.api.snapshot_download(repo_id=self.repo, repo_type="dataset",
                                                    allow_patterns=["runs/*.json", "events/*.json", "ledger.json"]))
        except Exception:
            log.exception("snapshot restore failed")
            self.restore_ok = False
            return 0
        restored = 0
        for f in sorted(local.glob("runs/*.json")):
            rec = RunRecord.model_validate_json(f.read_text())
            if self.store.get_run(rec.id) is None:
                self.store.save_run(rec)
                restored += 1
            ev_file = local / "events" / f"{rec.id}.json"
            if ev_file.exists() and not self.store.events_after(rec.id, 0):
                for ev in json.loads(ev_file.read_text()):
                    self.store.append_event(rec.id, ev)
        ledger_file = local / "ledger.json"
        if ledger_file.exists():
            self.ledger.import_rows(json.loads(ledger_file.read_text()))
        return restored
