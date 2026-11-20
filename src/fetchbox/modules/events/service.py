"""Event-stream sessions shared by the WebSocket and SSE endpoints.

A stream delivers events whose type matches the client's topic filter *and* that the
principal may see (``auth.*`` needs ``auth:admin``). It also watches for revocation of
the principal's own credential and ends the stream when that happens, so a revoked key
or a password change can't keep a long-lived connection open.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

from fetchbox.core.events import Event, EventBus, Subscription, matches
from fetchbox.modules.auth import scopes as sc
from fetchbox.modules.auth.service import Principal

MAX_TOPICS = 20
KEEPALIVE_S = 15.0
_TOPIC_RE = re.compile(r"^[a-z0-9_*]+(\.[a-z0-9_*]+)*$")

# Topic prefix -> extra scope needed to receive it (beyond events:read).
RESTRICTED: dict[str, str] = {"auth.": sc.ADMIN}

_KEY_REVOKING = {"auth.key_revoked", "auth.key_deleted", "auth.key_rotated"}


class TopicError(ValueError):
    pass


def parse_topics(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return ("*",)
    topics = tuple(t.strip() for t in raw.split(",") if t.strip())
    if len(topics) > MAX_TOPICS:
        raise TopicError(f"at most {MAX_TOPICS} topics")
    bad = [t for t in topics if t != "*" and not _TOPIC_RE.match(t)]
    if bad:
        raise TopicError(f"invalid topic pattern(s): {', '.join(bad)}")
    return topics


def visible_to(principal: Principal, event_type: str) -> bool:
    return all(
        principal.has(scope)
        for prefix, scope in RESTRICTED.items()
        if event_type.startswith(prefix)
    )


def revokes(principal: Principal, event: Event) -> bool:
    if event.subject is None or event.subject.id != principal.id:
        return False
    if principal.type == "api_key":
        return event.type in _KEY_REVOKING
    return event.type == "auth.password_changed"


@dataclass
class Item:
    kind: Literal["event", "gap", "revoked", "keepalive"]
    event: Event | None = None


@dataclass
class EventStream:
    bus: EventBus
    principal: Principal
    topics: tuple[str, ...]
    last_event_id: str | None = None
    keepalive_s: float = KEEPALIVE_S
    _sub: Subscription | None = field(default=None, init=False)
    _replayed: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self.gap = bool(self.last_event_id) and self.bus.may_have_gap(self.last_event_id or "")
        if self.last_event_id:
            # Replayed history must not end the stream (e.g. reconnecting with a freshly
            # rotated key would otherwise replay its own auth.key_rotated).
            self._replayed = {e.id for e in self.bus.replay_after(self.last_event_id)}
        # Subscribe to everything: revocation events must be seen even if filtered out.
        self._sub = self.bus.subscribe("*", last_event_id=self.last_event_id)

    def close(self) -> None:
        if self._sub is not None:
            self._sub.close()
            self._sub = None

    async def items(self) -> AsyncIterator[Item]:
        """Yields events to deliver; ends after a `revoked` item."""
        assert self._sub is not None
        if self.gap:
            yield Item("gap")
        queue = self._sub.queue
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), self.keepalive_s)
                except TimeoutError:
                    yield Item("keepalive")
                    continue
                if ev.id not in self._replayed and revokes(self.principal, ev):
                    yield Item("revoked", ev)
                    return
                if matches(ev.type, self.topics) and visible_to(self.principal, ev.type):
                    yield Item("event", ev)
        finally:
            self.close()


def envelope_json(ev: Event) -> str:
    return ev.model_dump_json()


def sse_format(item: Item) -> str:
    if item.kind == "keepalive":
        return ": keepalive\n\n"
    if item.kind == "gap":
        return "event: stream.gap\ndata: {}\n\n"
    if item.kind == "revoked":
        return f"event: stream.revoked\ndata: {json.dumps({'reason': 'credential revoked'})}\n\n"
    assert item.event is not None
    return f"id: {item.event.id}\nevent: {item.event.type}\ndata: {envelope_json(item.event)}\n\n"
