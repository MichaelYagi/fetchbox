"""In-process async event bus.

One envelope shape is shared by WebSocket, SSE, webhooks and the activity log.
Subscribers filter by topic patterns like ``download.*``; a ring buffer of the last
1,000 events supports replay from ``Last-Event-ID`` after a reconnect.
"""

from __future__ import annotations

import asyncio
import fnmatch
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from fetchbox.core.ids import new_id

log = structlog.get_logger(__name__)

REPLAY_BUFFER = 1000
SUBSCRIBER_QUEUE = 1000


class Actor(BaseModel):
    type: Literal["user", "api_key", "system", "job"] = "system"
    id: str | None = None
    name: str | None = None


class Subject(BaseModel):
    type: str
    id: str


class Event(BaseModel):
    id: str = Field(default_factory=new_id)
    type: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: Actor = Field(default_factory=Actor)
    subject: Subject | None = None
    data: dict[str, Any] = Field(default_factory=dict)


Handler = Callable[[Event], Awaitable[None]]


def matches(event_type: str, patterns: Iterable[str]) -> bool:
    return any(p == "*" or fnmatch.fnmatchcase(event_type, p) for p in patterns)


class Subscription:
    def __init__(self, bus: EventBus, patterns: tuple[str, ...]) -> None:
        self._bus = bus
        self.patterns = patterns
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
        self.dropped = 0

    def offer(self, event: Event) -> None:
        if not matches(event.type, self.patterns):
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Slow consumer: drop oldest so the bus never blocks publishers.
            self.dropped += 1
            self.queue.get_nowait()
            self.queue.put_nowait(event)

    def close(self) -> None:
        self._bus._subs.discard(self)

    async def __aiter__(self) -> AsyncIterator[Event]:
        try:
            while True:
                yield await self.queue.get()
        finally:
            self.close()


class EventBus:
    def __init__(self, replay_size: int = REPLAY_BUFFER) -> None:
        self._buffer: deque[Event] = deque(maxlen=replay_size)
        self._subs: set[Subscription] = set()
        self._handlers: list[tuple[tuple[str, ...], Handler]] = []

    def on(self, *patterns: str) -> Callable[[Handler], Handler]:
        """Register an in-process handler (e.g. library subscribes to download.completed)."""

        def deco(fn: Handler) -> Handler:
            self._handlers.append((patterns or ("*",), fn))
            return fn

        return deco

    async def publish(self, event: Event) -> Event:
        self._buffer.append(event)
        for sub in list(self._subs):
            sub.offer(event)
        for patterns, handler in self._handlers:
            if matches(event.type, patterns):
                try:
                    await handler(event)
                except Exception:
                    log.exception("event_handler_failed", event_type=event.type, handler=handler)
        return event

    async def emit(
        self,
        type: str,
        *,
        subject: Subject | None = None,
        actor: Actor | None = None,
        **data: Any,
    ) -> Event:
        return await self.publish(
            Event(type=type, subject=subject, actor=actor or Actor(), data=data)
        )

    def subscribe(self, *patterns: str, last_event_id: str | None = None) -> Subscription:
        sub = Subscription(self, patterns or ("*",))
        if last_event_id is not None:
            for ev in self.replay_after(last_event_id):
                sub.offer(ev)
        self._subs.add(sub)
        return sub

    def replay_after(self, last_event_id: str) -> list[Event]:
        """Events after `last_event_id`. ULIDs sort by time, so compare lexically."""
        return [e for e in self._buffer if e.id > last_event_id]

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
