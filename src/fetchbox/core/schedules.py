"""Recurring jobs as editable rows.

Modules *define* schedules in code (name, job type, default interval). On startup a row
is created for each new definition; existing rows keep the user's edits (interval,
enabled). The scheduler loop enqueues due schedules with ``dedupe_key=schedule:<name>``
so a slow run never piles up duplicates, then sets the next run to ``now + interval``
(missed runs while offline collapse into one, never a burst).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import JSON, Boolean, Integer, String, Text, select, update
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.concurrency import VersionMixin
from fetchbox.core.db import Base, Database, UTCDateTime, utcnow
from fetchbox.core.jobs import Job, JobRunner

log = structlog.get_logger(__name__)

MIN_INTERVAL = timedelta(seconds=30)
MAX_INTERVAL = timedelta(days=90)
TICK_S = 1.0


class Schedule(VersionMixin, Base):
    __tablename__ = "schedule"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    interval_seconds: Mapped[int] = mapped_column(Integer)
    default_interval_seconds: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_job_id: Mapped[str | None] = mapped_column(String(26), nullable=True)


@dataclass(frozen=True)
class ScheduleDef:
    name: str
    job_type: str
    every: timedelta
    description: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    run_at_start: bool = False
    enabled: bool = True


def validate_interval(seconds: int) -> None:
    if not MIN_INTERVAL.total_seconds() <= seconds <= MAX_INTERVAL.total_seconds():
        raise ValueError(
            f"interval must be between {int(MIN_INTERVAL.total_seconds())}s "
            f"and {int(MAX_INTERVAL.total_seconds())}s"
        )


class Scheduler:
    def __init__(self, db: Database, jobs: JobRunner, *, tick_s: float = TICK_S) -> None:
        self.db = db
        self.jobs = jobs
        self.tick_s = tick_s
        self._defs: dict[str, ScheduleDef] = {}
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    def define(self, d: ScheduleDef) -> None:
        if d.name in self._defs:
            raise ValueError(f"schedule {d.name!r} already defined")
        validate_interval(int(d.every.total_seconds()))
        self._defs[d.name] = d

    def undefine(self, name: str) -> None:
        """Stop firing a schedule (a plugin being unloaded); its row and settings stay, so
        it carries on as it was when it's defined again."""
        self._defs.pop(name, None)

    @property
    def definitions(self) -> dict[str, ScheduleDef]:
        return dict(self._defs)

    async def sync(self) -> None:
        """Create rows for new definitions; refresh job type/description of existing ones."""
        now = utcnow()
        async with self.db.sessionmaker() as s:
            existing = {r.name: r for r in (await s.execute(select(Schedule))).scalars()}
            for d in self._defs.values():
                secs = int(d.every.total_seconds())
                row = existing.get(d.name)
                if row is None:
                    s.add(
                        Schedule(
                            name=d.name,
                            job_type=d.job_type,
                            description=d.description,
                            interval_seconds=secs,
                            default_interval_seconds=secs,
                            enabled=d.enabled,
                            payload=d.payload,
                            next_run_at=now if d.run_at_start else now + d.every,
                        )
                    )
                else:
                    row.job_type, row.description = d.job_type, d.description
                    row.default_interval_seconds = secs
            await s.commit()

    async def start(self) -> None:
        await self.sync()
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def poke(self) -> None:
        """Re-evaluate now (after an edit)."""
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due()
            except Exception:
                log.exception("scheduler_tick_failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.tick_s)

    async def run_due(self) -> list[str]:
        now = utcnow()
        async with self.db.sessionmaker() as s:
            due = list(
                (
                    await s.execute(
                        select(Schedule).where(
                            Schedule.enabled.is_(True), Schedule.next_run_at <= now
                        )
                    )
                ).scalars()
            )
        fired = []
        for row in due:
            if row.name not in self._defs:
                continue  # defined by a module that is no longer loaded
            job = await self._enqueue(row)
            values: dict[str, Any] = {
                "last_run_at": now,
                "next_run_at": now + timedelta(seconds=row.interval_seconds),
            }
            if job is not None:
                values["last_job_id"] = job.id
            await self._bookkeep(row.name, values)
            fired.append(row.name)
        return fired

    async def _bookkeep(self, name: str, values: dict[str, Any]) -> None:
        """Run bookkeeping is not a user edit: Core UPDATE, so the row version (ETag) and
        a client's pending If-Match stay valid."""
        async with self.db.sessionmaker() as s:
            await s.execute(update(Schedule).where(Schedule.name == name).values(**values))
            await s.commit()

    async def run_now(self, name: str) -> Job | None:
        """Manual trigger. Doesn't move the regular next run. None if one is already active."""
        async with self.db.sessionmaker() as s:
            row = await s.get(Schedule, name)
        if row is None:
            raise LookupError(name)
        job = await self._enqueue(row)
        if job is not None:
            await self._bookkeep(name, {"last_job_id": job.id})
        return job

    async def _enqueue(self, row: Schedule) -> Job | None:
        return await self.jobs.enqueue(
            row.job_type, dict(row.payload), dedupe_key=f"schedule:{row.name}"
        )
