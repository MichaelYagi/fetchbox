"""Activity log: one human-readable audit line per meaningful event.

The writer subscribes to the bus and inserts entries in batches from a background task,
so publishing an event never waits on a database write. High-frequency events (progress
ticks, routine job lifecycle) are skipped; failures and every auth change are kept.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import JSON, Index, String, Text, delete
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.db import Base, Database, IdMixin, UTCDateTime, utcnow
from fetchbox.core.events import Event, EventBus, Subscription, matches

log = structlog.get_logger(__name__)

SKIP = ("*.progress", "job.queued", "job.started", "job.succeeded")
BATCH = 200
DEFAULT_RETENTION = timedelta(days=90)

FAILURE_ACTIONS = {"failed", "rejected", "offline", "low_space"}

# event type -> message template over event.data (missing keys render as "?")
MESSAGES: dict[str, str] = {
    "system.started": "Fetchbox {version} started",
    "system.stopping": "Fetchbox is shutting down",
    "system.health_changed": "Overall health is now {status}",
    "auth.key_created": "API key '{name}' ({prefix}) created",
    "auth.key_rotated": "API key '{name}' rotated (new prefix {prefix})",
    "auth.key_revoked": "API key '{name}' ({prefix}) revoked",
    "auth.key_deleted": "API key '{name}' ({prefix}) deleted",
    "auth.key_scopes_changed": "API key '{name}' scopes changed",
    "auth.password_changed": "Admin password changed for '{username}'",
    "job.failed": "Job {job_type} failed: {error}",
    "job.cancelled": "Job {job_type} cancelled",
    "profiles.profile_created": "Quality profile '{name}' created",
    "profiles.profile_updated": "Quality profile '{name}' updated",
    "profiles.profile_deleted": "Quality profile '{name}' deleted",
    "profiles.format_created": "Custom format '{name}' created",
    "profiles.format_updated": "Custom format '{name}' updated",
    "profiles.format_deleted": "Custom format '{name}' deleted",
    "system.config_changed": "Settings changed: {fields}",
    "title.added": "Added {name} ({year})",
    "title.updated": "Updated {name} ({year})",
    "title.refreshed": "Refreshed metadata for {name} ({year})",
    "title.removed": "Removed {name} ({year})",
    "wanted.updated": "Monitoring for {name} set to {monitored}",
}


class ActivityEntry(IdMixin, Base):
    __tablename__ = "activity"

    category: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subject_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        Index("ix_activity_category", "category"),
        Index("ix_activity_subject", "subject_type", "subject_id"),
        Index("ix_activity_created_at", "created_at"),
    )


class _Default(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "?"


def entry_for(ev: Event) -> ActivityEntry | None:
    if matches(ev.type, SKIP):
        return None
    category, _, action = ev.type.partition(".")
    action = action or "event"
    template = MESSAGES.get(ev.type)
    message = template.format_map(_Default(ev.data)) if template else ev.type
    actor = ev.actor.name or ev.actor.type
    return ActivityEntry(
        id=ev.id,  # one entry per event; also makes writes idempotent
        category=category,
        action=action,
        actor=actor,
        subject_type=ev.subject.type if ev.subject else None,
        subject_id=ev.subject.id if ev.subject else None,
        outcome="failure" if action in FAILURE_ACTIONS else "success",
        message=message,
        data={**ev.data, "actor": ev.actor.model_dump(exclude_none=True)},
        created_at=ev.occurred_at,
    )


class ActivityWriter:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus
        self._sub: Subscription | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._in_hand: list[Event] = []  # taken off the queue, not yet written
        self.written = 0

    def start(self) -> None:
        self._sub = self.bus.subscribe("*")
        self._task = asyncio.create_task(self._run(), name="activity-writer")

    async def stop(self) -> None:
        """Flush everything queued or in hand, then stop. Never drops a taken event."""
        if self._task is not None:
            async with self._lock:  # never cancel in the middle of a write
                self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._sub is not None:
            while self._in_hand or not self._sub.queue.empty():
                self._in_hand.extend(self._drain())
                await self._write_in_hand()
            self._sub.close()
            self._sub = None

    def _drain(self) -> list[Event]:
        assert self._sub is not None
        out: list[Event] = []
        while not self._sub.queue.empty() and len(out) < BATCH:
            out.append(self._sub.queue.get_nowait())
        return out

    async def _run(self) -> None:
        assert self._sub is not None
        while True:
            self._in_hand.append(await self._sub.queue.get())
            self._in_hand.extend(self._drain())
            async with self._lock:
                await self._write_in_hand()

    async def _write_in_hand(self) -> None:
        batch, self._in_hand = self._in_hand, []
        await self._flush(batch)

    async def _flush(self, events: list[Event]) -> None:
        entries = [e for e in (entry_for(ev) for ev in events) if e is not None]
        if not entries:
            return
        try:
            async with self.db.sessionmaker() as s:
                for entry in entries:
                    await s.merge(entry)
                await s.commit()
            self.written += len(entries)
        except Exception:
            log.exception("activity_write_failed", count=len(entries))


async def prune(db: Database, older_than: timedelta = DEFAULT_RETENTION) -> int:
    async with db.sessionmaker() as s:
        res = await s.execute(
            delete(ActivityEntry).where(ActivityEntry.created_at < utcnow() - older_than)
        )
        await s.commit()
    return int(res.rowcount or 0)  # type: ignore[attr-defined]
