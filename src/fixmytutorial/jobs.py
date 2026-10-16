from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Callable


class RateLimiter:
    def __init__(self, per_hour: int, clock: Callable[[], float] = time.monotonic):
        self.per_hour = per_hour
        self._clock = clock
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= 3600:
            hits.popleft()
        if len(hits) >= self.per_hour:
            return False
        hits.append(now)
        return True


class StreamLimiter:
    """Caps open live-progress streams per visitor and in total, so one visitor can't tie up the server."""

    def __init__(self, per_client: int, total: int):
        self.per_client = per_client
        self.total = total
        self._open: dict[str, int] = {}
        self._count = 0

    def acquire(self, key: str) -> bool:
        if self._count >= self.total or self._open.get(key, 0) >= self.per_client:
            return False
        self._open[key] = self._open.get(key, 0) + 1
        self._count += 1
        return True

    def release(self, key: str) -> None:
        n = self._open.get(key, 0)
        if n <= 0:
            return
        if n == 1:
            del self._open[key]
        else:
            self._open[key] = n - 1
        self._count -= 1


class JobQueue:
    def __init__(self, pipeline, store, bus, settings):
        self.pipeline = pipeline
        self.store = store
        self.bus = bus
        self._sem = asyncio.Semaphore(settings.max_concurrent_runs)
        self._waiting: list[str] = []
        self._tasks: set[asyncio.Task] = set()

    def queued(self) -> list[str]:
        return list(self._waiting)

    def submit(self, record, source) -> None:
        self.store.save_run(record)
        self._waiting.append(record.id)
        task = asyncio.create_task(self._run(record, source))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _publish_positions(self) -> None:
        for i, run_id in enumerate(self._waiting):
            await self.bus.publish(run_id, {"type": "queue", "position": i + 1})

    async def _run(self, record, source) -> None:
        if self._sem.locked():
            await self._publish_positions()
        async with self._sem:
            self._waiting.remove(record.id)
            await self.pipeline.run(record, source)
        if self._waiting:
            await self._publish_positions()

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks))
