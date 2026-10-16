import asyncio

from fixmytutorial.events import EventBus
from fixmytutorial.ingest import Source
from fixmytutorial.jobs import JobQueue, RateLimiter
from fixmytutorial.models import RunRecord, utcnow
from fixmytutorial.store import Store


def test_rate_limiter_window():
    t = {"now": 0.0}
    rl = RateLimiter(2, clock=lambda: t["now"])
    assert rl.allow("ip") and rl.allow("ip") and not rl.allow("ip")
    assert rl.allow("other")
    t["now"] = 3601
    assert rl.allow("ip")


class GatedPipeline:
    def __init__(self):
        self.gate = asyncio.Event()
        self.running = 0
        self.max_running = 0

    async def run(self, record, source):
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        await self.gate.wait()
        self.running -= 1
        return record


async def test_queue_limits_concurrency_and_reports_position(settings, tmp_path):
    store = Store(tmp_path / "s.db")
    bus = EventBus(store, ping_s=0.05)
    pipe = GatedPipeline()
    q = JobQueue(pipe, store, bus, settings.model_copy(update={"max_concurrent_runs": 2}))
    for i in range(3):
        q.submit(RunRecord(id=f"r{i}", source="x", created_at=utcnow()), Source(markdown="# t"))
    await asyncio.sleep(0.05)
    assert pipe.max_running == 2 and q.queued() == ["r2"]
    assert {"type": "queue", "position": 1} in [e for _, e in store.events_after("r2", 0)]
    assert store.get_run("r0") is not None
    pipe.gate.set()
    await q.drain()
    assert pipe.running == 0


def test_stream_limiter_caps_per_visitor_and_in_total():
    from fixmytutorial.jobs import StreamLimiter
    s = StreamLimiter(per_client=2, total=3)
    assert s.acquire("a") and s.acquire("a") and not s.acquire("a")
    assert s.acquire("b") and not s.acquire("c")  # total reached
    s.release("a")
    assert s.acquire("c")
    s.release("zzz")  # releasing an unknown key is harmless
    assert not s.acquire("d")
