from __future__ import annotations

import asyncio

from fetchbox.core.events import Event, EventBus, Subject, matches


def test_matches() -> None:
    assert matches("download.completed", ["download.*"])
    assert matches("download.completed", ["*"])
    assert not matches("import.completed", ["download.*"])


async def test_subscribe_filters_by_topic() -> None:
    bus = EventBus()
    sub = bus.subscribe("download.*")
    await bus.emit("import.started")
    ev = await bus.emit("download.completed", subject=Subject(type="download", id="d1"), size=10)
    got = await asyncio.wait_for(sub.queue.get(), 1)
    assert got.id == ev.id
    assert got.data == {"size": 10}
    assert sub.queue.empty()


async def test_replay_after_last_event_id() -> None:
    bus = EventBus(replay_size=5)
    events = [await bus.emit(f"t.{i}") for i in range(8)]
    # oldest 3 fell out of the buffer
    assert [e.id for e in bus.replay_after(events[0].id)] == [e.id for e in events[3:]]
    sub = bus.subscribe("t.*", last_event_id=events[5].id)
    assert [sub.queue.get_nowait().type for _ in range(2)] == ["t.6", "t.7"]


async def test_handler_failure_does_not_break_publish() -> None:
    bus = EventBus()
    seen: list[str] = []

    @bus.on("x.*")
    async def bad(e: Event) -> None:
        raise RuntimeError("boom")

    @bus.on("x.*")
    async def good(e: Event) -> None:
        seen.append(e.type)

    await bus.emit("x.y")
    assert seen == ["x.y"]


async def test_slow_subscriber_drops_oldest() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    for i in range(sub.queue.maxsize + 3):
        await bus.emit("e", i=i)
    assert sub.dropped == 3
    assert sub.queue.get_nowait().data["i"] == 3
