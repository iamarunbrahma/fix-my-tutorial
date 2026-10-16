from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .models import RunRecord, RunStatus, Verdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, created_at REAL, updated_at REAL, source TEXT, content_hash TEXT,
  public INTEGER, status TEXT, verdict TEXT, record TEXT
);
CREATE INDEX IF NOT EXISTS runs_source ON runs(source, updated_at);
CREATE TABLE IF NOT EXISTS events (run_id TEXT, seq INTEGER, ts REAL, data TEXT, PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, expires_at REAL);
"""


class Store:
    def __init__(self, db_path: str | Path, clock: Callable[[], float] = time.time):
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._lock = threading.Lock()
        self._clock = clock

    def save_run(self, rec: RunRecord, at: float | None = None) -> None:
        """Save or update a run. `at` keeps a restored run's own time, so a restart doesn't make old reports look
        new (or decide by load order which report is the most recent)."""
        now = self._clock() if at is None else at
        with self._lock:
            self._db.execute(
                """INSERT INTO runs (id, created_at, updated_at, source, content_hash, public, status, verdict, record)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, content_hash=excluded.content_hash,
                   status=excluded.status, verdict=excluded.verdict, record=excluded.record""",
                (rec.id, now, now, rec.source, rec.content_hash, int(rec.public), rec.status.value,
                 rec.verdict.value if rec.verdict else None, rec.model_dump_json()),
            )
            self._db.commit()

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._db.execute("SELECT record FROM runs WHERE id=?", (run_id,)).fetchone()
        return RunRecord.model_validate_json(row[0]) if row else None

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        with self._lock:
            rows = self._db.execute("SELECT record FROM runs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [RunRecord.model_validate_json(r[0]) for r in rows]

    def find_recent_run(self, source: str, within_s: int) -> RunRecord | None:
        since = self._clock() - within_s
        with self._lock:
            row = self._db.execute(
                """SELECT record FROM runs WHERE source=? AND updated_at>=? AND status=? AND verdict IS NOT NULL
                   AND verdict != ? ORDER BY updated_at DESC LIMIT 1""",
                (source, since, RunStatus.DONE.value, Verdict.ERROR.value),
            ).fetchone()
        return RunRecord.model_validate_json(row[0]) if row else None

    def append_event(self, run_id: str, data: dict) -> int:
        with self._lock:
            row = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id=?", (run_id,)).fetchone()
            seq = row[0] + 1
            self._db.execute("INSERT INTO events (run_id, seq, ts, data) VALUES (?,?,?,?)",
                             (run_id, seq, self._clock(), json.dumps(data)))
            self._db.commit()
        return seq

    def events_after(self, run_id: str, after_seq: int) -> list[tuple[int, dict]]:
        with self._lock:
            rows = self._db.execute("SELECT seq, data FROM events WHERE run_id=? AND seq>? ORDER BY seq",
                                    (run_id, after_seq)).fetchall()
        return [(seq, json.loads(data)) for seq, data in rows]

    def cache_get(self, key: str) -> Any | None:
        with self._lock:
            row = self._db.execute("SELECT value, expires_at FROM cache WHERE key=?", (key,)).fetchone()
        if not row or row[1] < self._clock():
            return None
        return json.loads(row[0])

    def cache_put(self, key: str, value: Any, ttl_s: int) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?,?,?)",
                             (key, json.dumps(value), self._clock() + ttl_s))
            self._db.commit()
