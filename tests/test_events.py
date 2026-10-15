import asyncio

from fixmytutorial.events import EventBus
from fixmytutorial.store import Store


async def collect(bus, run_id, after=0, limit=10):
    out = []
    async for seq, ev in bus.subscribe(run_id, after):
        out.append((seq, ev))
        if ev["type"] == "done" or len(out) >= limit:
            break
    return out


async def test_live_subscriber_receives_published_events(tmp_path):
    bus = EventBus(Store(tmp_path / "s.db"), ping_s=5)
    task = asyncio.create_task(collect(bus, "r1"))
    await asyncio.sleep(0.05)
    await bus.publish("r1", {"type": "stage", "stage": "planning"})
    await bus.publish("r1", {"type": "done"})
    got = await asyncio.wait_for(task, 2)
    assert [e["type"] for _, e in got] == ["stage", "done"]


async def test_late_subscriber_gets_full_history_then_stops(tmp_path):
    bus = EventBus(Store(tmp_path / "s.db"), ping_s=5)
    for t in ["stage", "step", "verdict", "done"]:
        await bus.publish("r1", {"type": t})
    got = await asyncio.wait_for(collect(bus, "r1"), 2)
    assert [e["type"] for _, e in got] == ["stage", "step", "verdict", "done"]


async def test_resume_after_seq(tmp_path):
    bus = EventBus(Store(tmp_path / "s.db"), ping_s=5)
    for t in ["a", "b", "done"]:
        await bus.publish("r1", {"type": t})
    got = await asyncio.wait_for(collect(bus, "r1", after=1), 2)
    assert [e["type"] for _, e in got] == ["b", "done"]


async def test_ping_when_idle(tmp_path):
    bus = EventBus(Store(tmp_path / "s.db"), ping_s=0.05)
    got = await asyncio.wait_for(collect(bus, "idle", limit=1), 2)
    assert got[0][1]["type"] == "ping"
