from __future__ import annotations

import asyncio

import pytest

from fetchbox.core.events import EventBus, Subject
from fetchbox.modules.auth.service import Principal
from fetchbox.modules.events.service import (
    EventStream,
    Item,
    TopicError,
    parse_topics,
    sse_format,
    visible_to,
)


def _p(scopes: set[str], type: str = "api_key", id: str = "k1") -> Principal:
    return Principal(type=type, id=id, name="t", scopes=frozenset(scopes), via="api_key")  # type: ignore[arg-type]


async def _take(stream: EventStream, n: int) -> list[Item]:
    out: list[Item] = []
    async for item in stream.items():
        if item.kind != "keepalive":
            out.append(item)
        if len(out) == n or item.kind == "revoked":
            break
    return out


def test_parse_topics() -> None:
    assert parse_topics(None) == ("*",)
    assert parse_topics(" download.* , job.failed ") == ("download.*", "job.failed")
    for bad in ("Download.*", "a..b", "x;y", ",".join(["a"] * 21)):
        with pytest.raises(TopicError):
            parse_topics(bad)


def test_auth_events_need_admin() -> None:
    assert not visible_to(_p({"events:read"}), "auth.key_created")
    assert visible_to(_p({"events:read", "auth:admin"}), "auth.key_created")
    assert visible_to(_p({"events:read"}), "job.failed")


async def test_filters_topics_and_visibility() -> None:
    bus = EventBus()
    stream = EventStream(bus, _p({"events:read"}), ("job.*", "auth.*"), keepalive_s=0.05)
    await bus.emit("download.completed")
    await bus.emit("auth.key_created", subject=Subject(type="api_key", id="other"))
    await bus.emit("job.failed")
    items = await asyncio.wait_for(_take(stream, 1), 2)
    assert [i.event.type for i in items if i.event] == ["job.failed"]


async def test_own_key_revocation_ends_stream() -> None:
    bus = EventBus()
    stream = EventStream(bus, _p({"events:read"}, id="k1"), ("*",), keepalive_s=0.05)
    await bus.emit("auth.key_revoked", subject=Subject(type="api_key", id="someone-else"))
    await bus.emit("job.failed")
    await bus.emit("auth.key_revoked", subject=Subject(type="api_key", id="k1"))
    items = await asyncio.wait_for(_take(stream, 5), 2)
    assert [i.kind for i in items] == ["event", "revoked"]


async def test_password_change_ends_user_stream() -> None:
    bus = EventBus()
    user = _p({"*"}, type="user", id="u1")
    stream = EventStream(bus, user, ("*",), keepalive_s=0.05)
    await bus.emit("auth.password_changed", subject=Subject(type="user", id="u1"))
    items = await asyncio.wait_for(_take(stream, 5), 2)
    assert items[-1].kind == "revoked"


async def test_replay_and_replayed_revocation_ignored() -> None:
    bus = EventBus()
    first = await bus.emit("job.failed", n=1)
    await bus.emit("auth.key_rotated", subject=Subject(type="api_key", id="k1"))
    await bus.emit("job.failed", n=2)
    stream = EventStream(
        bus, _p({"*"}, id="k1"), ("job.*",), last_event_id=first.id, keepalive_s=0.05
    )
    assert not stream.gap
    items = await asyncio.wait_for(_take(stream, 1), 2)
    assert items[0].event is not None and items[0].event.data == {"n": 2}


async def test_gap_signalled_when_buffer_overflowed() -> None:
    bus = EventBus(replay_size=3)
    old = await bus.emit("job.failed")
    for _ in range(5):
        await bus.emit("job.failed")
    stream = EventStream(bus, _p({"*"}), ("*",), last_event_id=old.id, keepalive_s=0.05)
    items = await asyncio.wait_for(_take(stream, 2), 2)
    assert items[0].kind == "gap"


def test_sse_format() -> None:
    from fetchbox.core.events import Event

    ev = Event(type="job.failed", data={"x": 1})
    text = sse_format(Item("event", ev))
    assert text.startswith(f"id: {ev.id}\nevent: job.failed\ndata: {{")
    assert text.endswith("\n\n")
    assert sse_format(Item("keepalive")) == ": keepalive\n\n"
