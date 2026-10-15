"""Per-run event stream. Events are stored first, so a refresh or a late visitor replays everything."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import AsyncIterator

from .store import Store


class EventBus:
    def __init__(self, store: Store, ping_s: float = 15.0):
        self._store = store
        self._ping_s = ping_s
        self._conds: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)

    async def publish(self, run_id: str, event: dict) -> int:
        cond = self._conds[run_id]
        async with cond:
            seq = self._store.append_event(run_id, event)
            cond.notify_all()
        return seq

    async def subscribe(self, run_id: str, after: int = 0) -> AsyncIterator[tuple[int, dict]]:
        last = after
        while True:
            cond = self._conds[run_id]
            async with cond:
                batch = self._store.events_after(run_id, last)
                if not batch:
                    try:
                        await asyncio.wait_for(cond.wait(), timeout=self._ping_s)
                    except TimeoutError:
                        pass
                    batch = self._store.events_after(run_id, last)
            if not batch:
                yield last, {"type": "ping"}
                continue
            for seq, ev in batch:
                last = seq
                yield seq, ev
                if ev.get("type") == "done":
                    return
