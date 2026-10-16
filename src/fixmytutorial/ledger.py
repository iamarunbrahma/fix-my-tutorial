"""Spend guard. Every model and Tavily call is reserved here first, then committed with real usage.

The Token Factory card is charged automatically if the balance goes negative, so these caps are
the only thing standing between a busy demo and a card charge. Keep them conservative.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
  id TEXT PRIMARY KEY, ts REAL, day TEXT, run_id TEXT, public INTEGER, kind TEXT,
  role TEXT, model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, credits INTEGER, usd REAL
)
"""
COLUMNS = ("id", "ts", "day", "run_id", "public", "kind", "role", "model",
           "prompt_tokens", "completion_tokens", "credits", "usd")


class BudgetExceeded(Exception):
    def __init__(self, scope: str, message: str):
        super().__init__(message)
        self.scope = scope


@dataclass
class Reservation:
    id: str
    run_id: str
    public: bool
    kind: str  # "llm" | "tavily"
    amount: float  # usd for llm, credits for tavily


@dataclass
class Totals:
    lifetime_usd: float
    today_public_usd: float
    today_all_usd: float
    run_usd: float
    lifetime_credits: int
    run_credits: int


class Ledger:
    def __init__(self, db_path: str | Path, settings: Settings, clock: Callable[[], float] = time.time):
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute(SCHEMA)
        self._db.commit()
        self._lock = threading.Lock()
        self._s = settings
        self._clock = clock
        self._pending: dict[str, Reservation] = {}
        self._unverified: list[str] = []  # why past spend can't be trusted (each cleared on its own); runs stay closed

    def _day(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m-%d")

    def _reserve_held(self) -> bool:
        today = datetime.fromtimestamp(self._clock(), timezone.utc).date()
        return self._s.public_demo and today < self._s.demo_reserve_until

    def public_cap_usd(self) -> float:
        """What public runs may spend in total: the demo holds part of its budget back until judging starts."""
        return self._s.cap_lifetime_usd - (self._s.demo_reserve_usd if self._reserve_held() else 0.0)

    def _reserve_message(self) -> str:
        d = self._s.demo_reserve_until
        return (f"Public runs are paused until {d.day} {d:%B}. "
                "You can still browse the Rot Index and open past reports.")

    def price_usd(self, role: str, prompt_tokens: int, completion_tokens: int) -> float:
        pin, pout = self._s.price_for(role)
        return (prompt_tokens / 1e6 * pin + completion_tokens / 1e6 * pout) * (1 + self._s.tax_rate)

    def _sum(self, sql: str, args: tuple = ()) -> float:
        row = self._db.execute(sql, args).fetchone()
        return float(row[0] or 0)

    def _totals_unlocked(self, run_id: str | None) -> Totals:
        day = self._day()
        t = Totals(
            lifetime_usd=self._sum("SELECT SUM(usd) FROM spend"),
            today_public_usd=self._sum("SELECT SUM(usd) FROM spend WHERE day=? AND public=1", (day,)),
            today_all_usd=self._sum("SELECT SUM(usd) FROM spend WHERE day=?", (day,)),
            run_usd=self._sum("SELECT SUM(usd) FROM spend WHERE run_id=?", (run_id,)) if run_id else 0.0,
            lifetime_credits=int(self._sum("SELECT SUM(credits) FROM spend")),
            run_credits=int(self._sum("SELECT SUM(credits) FROM spend WHERE run_id=?", (run_id,))) if run_id else 0,
        )
        for r in self._pending.values():
            if r.kind == "llm":
                t.lifetime_usd += r.amount
                t.today_all_usd += r.amount
                if r.public:
                    t.today_public_usd += r.amount
                if run_id and r.run_id == run_id:
                    t.run_usd += r.amount
            else:
                t.lifetime_credits += int(r.amount)
                if run_id and r.run_id == run_id:
                    t.run_credits += int(r.amount)
        return t

    def totals(self, run_id: str | None = None) -> Totals:
        with self._lock:
            return self._totals_unlocked(run_id)

    def reserve_llm(self, run_id: str, public: bool, role: str, prompt_tokens_est: int,
                    max_tokens: int) -> Reservation:
        est = self.price_usd(role, prompt_tokens_est, max_tokens)
        with self._lock:
            t = self._totals_unlocked(run_id)
            if t.lifetime_usd + est > self._s.cap_lifetime_usd:
                raise BudgetExceeded("lifetime", "The project's total model budget is used up.")
            if t.today_all_usd + est > self._s.cap_daily_all_usd:
                raise BudgetExceeded("daily_all", "Today's model budget is used up. Try again tomorrow.")
            if public and t.lifetime_usd + est > self.public_cap_usd():
                raise BudgetExceeded("reserve", self._reserve_message())
            if public and t.today_public_usd + est > self._s.cap_daily_usd:
                raise BudgetExceeded("daily", "Today's budget for public runs is used up. Try again tomorrow.")
            if t.run_usd + est > self._s.cap_run_usd:
                raise BudgetExceeded("run", "This run reached its spending limit.")
            r = Reservation(uuid.uuid4().hex, run_id, public, "llm", est)
            self._pending[r.id] = r
            return r

    def reserve_tavily(self, run_id: str, public: bool, credits: int) -> Reservation:
        with self._lock:
            t = self._totals_unlocked(run_id)
            if t.lifetime_credits + credits > self._s.cap_tavily_lifetime_credits:
                raise BudgetExceeded("tavily_lifetime", "The project's web search budget is used up.")
            if t.run_credits + credits > self._s.cap_tavily_run_credits:
                raise BudgetExceeded("tavily_run", "This run reached its web search limit.")
            r = Reservation(uuid.uuid4().hex, run_id, public, "tavily", credits)
            self._pending[r.id] = r
            return r

    def _insert(self, r: Reservation, role: str, model: str, pt: int, ct: int, credits: int, usd: float) -> None:
        self._db.execute(
            f"INSERT INTO spend ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
            (r.id, self._clock(), self._day(), r.run_id, int(r.public), r.kind, role, model, pt, ct, credits, usd),
        )
        self._db.commit()

    def commit_llm(self, res: Reservation, role: str, model: str, prompt_tokens: int,
                   completion_tokens: int) -> float:
        usd = max(0.0, self.price_usd(role, prompt_tokens, completion_tokens))
        with self._lock:
            self._pending.pop(res.id, None)
            self._insert(res, role, model, prompt_tokens, completion_tokens, 0, usd)
        return usd

    def commit_tavily(self, res: Reservation, credits_used: int) -> None:
        with self._lock:
            self._pending.pop(res.id, None)
            self._insert(res, "search", "tavily", 0, 0, credits_used, 0.0)

    def charge_reserved(self, res: Reservation, role: str, model: str) -> float:
        """Record the full reservation as spent. Used when a call failed in a way that may still have been billed
        (timeouts, dropped connections, server errors), so the guard fails closed instead of open."""
        with self._lock:
            self._pending.pop(res.id, None)
            if res.kind == "llm":
                self._insert(res, role, model, -1, -1, 0, res.amount)
                return res.amount
            self._insert(res, role, model, 0, 0, int(res.amount), 0.0)
            return 0.0

    def release(self, res: Reservation) -> None:
        with self._lock:
            self._pending.pop(res.id, None)

    def mark_unverified(self, reason: str) -> None:
        if reason not in self._unverified:
            self._unverified.append(reason)

    def clear_unverified(self, reason: str) -> None:
        if reason in self._unverified:
            self._unverified.remove(reason)

    def unverified(self) -> list[str]:
        return list(self._unverified)

    def public_runs_open(self) -> tuple[bool, str]:
        if self._unverified:
            return False, f"Public runs are paused: {self._unverified[0]}"
        t = self.totals()

        def full(spent: float, cap: float) -> bool:  # a new run must be able to spend up to its own cap
            return spent >= cap or cap - spent < min(self._s.cap_run_usd, cap / 2)

        if full(t.lifetime_usd, self._s.cap_lifetime_usd):
            return False, "Public runs are paused: the project's model budget is used up."
        if full(t.lifetime_usd, self.public_cap_usd()):
            return False, self._reserve_message()
        if full(t.today_public_usd, self._s.cap_daily_usd) or full(t.today_all_usd, self._s.cap_daily_all_usd):
            return False, "Public runs are paused for today. Browse the Rot Index or come back tomorrow."
        if t.lifetime_credits >= self._s.cap_tavily_lifetime_credits:
            return False, "Public runs are paused: the web search budget is used up."
        return True, ""

    def export_rows(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(f"SELECT {','.join(COLUMNS)} FROM spend").fetchall()
        return [dict(zip(COLUMNS, row)) for row in rows]

    def import_rows(self, rows: list[dict]) -> int:
        with self._lock:
            before = self._db.total_changes
            self._db.executemany(
                f"INSERT OR IGNORE INTO spend ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                [tuple(r[c] for c in COLUMNS) for r in rows],
            )
            self._db.commit()
            return self._db.total_changes - before
