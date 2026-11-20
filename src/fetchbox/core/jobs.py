"""Persisted background jobs.

Jobs live in the ``job`` table so they survive restarts without Redis or Celery.

- Workers claim ready jobs with one atomic UPDATE that sets ``locked_by`` and a lease
  (``locked_until``), renewed by a heartbeat while the handler runs.
- Each queue has its own worker pool so one slow job kind can't starve the others.
- ``dedupe_key`` allows at most one pending/running job per key (partial unique index).
- Failures retry with exponential backoff + jitter up to ``max_attempts``.
- Cancellation is cooperative: handlers call ``ctx.check_cancelled()``; the heartbeat
  also cancels the task at its next await.
- Graceful stop waits for running jobs, then checkpoints the rest back to ``pending``.
- Startup recovery returns jobs left ``running`` by a dead process to ``pending``.
  Fetchbox is single-instance per database, so any running job not owned by this
  process is orphaned, whatever its lease says.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Text, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from fetchbox.core.db import Base, Database, IdMixin, UTCDateTime, utcnow
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.ids import new_id

log = structlog.get_logger(__name__)

JobStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]
ACTIVE: tuple[JobStatus, ...] = ("pending", "running")
FINISHED: tuple[JobStatus, ...] = ("succeeded", "failed", "cancelled")

# Spec defaults: io 2, net 4, client 1, maintenance 1.
DEFAULT_POOLS: dict[str, int] = {"io": 2, "net": 4, "client": 1, "maintenance": 1}

LEASE = timedelta(minutes=5)
POLL_INTERVAL_S = 1.0
BACKOFF_BASE_S = 10.0
BACKOFF_MAX_S = 3600.0
PROGRESS_EVENT_MIN_INTERVAL_S = 1.0


class Job(IdMixin, Base):
    __tablename__ = "job"

    type: Mapped[str] = mapped_column(String(100))
    queue: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    parent_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("job.id", ondelete="SET NULL"), nullable=True
    )
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        Index("ix_job_ready", "queue", "status", "run_after"),
        Index("ix_job_parent", "parent_id"),
        Index(
            "uq_job_dedupe_active",
            "dedupe_key",
            unique=True,
            sqlite_where=text("status IN ('pending','running')"),
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )


class JobCancelled(Exception):
    pass


class PermanentJobError(Exception):
    """Raise from a handler to fail without retrying."""


@dataclass
class JobContext:
    job_id: str
    type: str
    payload: dict[str, Any]
    attempt: int
    runner: JobRunner

    async def progress(self, value: float, message: str | None = None) -> None:
        await self.runner._report_progress(self.job_id, self.type, value, message)

    async def check_cancelled(self) -> None:
        if await self.runner._is_cancel_requested(self.job_id):
            raise JobCancelled

    async def enqueue_child(
        self, type: str, payload: dict[str, Any] | None = None, **kw: Any
    ) -> Job | None:
        return await self.runner.enqueue(type, payload, parent_id=self.job_id, **kw)


Handler = Callable[[JobContext], Coroutine[Any, Any, dict[str, Any] | None]]


@dataclass(frozen=True)
class HandlerSpec:
    type: str
    queue: str
    max_attempts: int
    fn: Handler


def backoff_delay(attempt: int) -> float:
    """Seconds before retry `attempt` (1-based): 10s, 20s, 40s ... capped at 1h, ±20% jitter."""
    base = min(BACKOFF_BASE_S * (2 ** max(attempt - 1, 0)), BACKOFF_MAX_S)
    return float(base * random.uniform(0.8, 1.2))


class JobRunner:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        pools: dict[str, int] | None = None,
        *,
        lease: timedelta = LEASE,
        poll_interval: float = POLL_INTERVAL_S,
    ) -> None:
        self.db = db
        self.bus = bus
        self.pools = dict(DEFAULT_POOLS if pools is None else pools)
        self.lease = lease
        self.poll_interval = poll_interval
        self.worker_id = f"w-{new_id()}"
        self._handlers: dict[str, HandlerSpec] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, asyncio.Task[Any]] = {}
        self._stopping = False
        self._last_progress_emit: dict[str, float] = {}

    # ---- registration ------------------------------------------------------------

    def handler(
        self, type: str, *, queue: str = "io", max_attempts: int = 3
    ) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self.register(type, fn, queue=queue, max_attempts=max_attempts)
            return fn

        return deco

    def register(self, type: str, fn: Handler, *, queue: str = "io", max_attempts: int = 3) -> None:
        if queue not in self.pools:
            raise ValueError(f"unknown queue {queue!r}; known: {sorted(self.pools)}")
        if type in self._handlers:
            raise ValueError(f"job handler {type!r} already registered")
        self._handlers[type] = HandlerSpec(type, queue, max_attempts, fn)

    # ---- enqueue / control -------------------------------------------------------

    async def enqueue(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        parent_id: str | None = None,
        max_attempts: int | None = None,
    ) -> Job | None:
        """Queue a job. Returns None when `dedupe_key` matches an active job."""
        spec = self._handlers.get(type)
        if spec is None:
            raise LookupError(f"no job handler registered for {type!r}")
        job = Job(
            id=new_id(),
            type=type,
            queue=spec.queue,
            status="pending",
            payload=payload or {},
            max_attempts=max_attempts or spec.max_attempts,
            run_after=run_after or utcnow(),
            parent_id=parent_id,
            dedupe_key=dedupe_key,
            created_at=utcnow(),
        )
        async with self.db.sessionmaker() as s:
            s.add(job)
            try:
                await s.commit()
            except IntegrityError:
                if dedupe_key is None:
                    raise
                await s.rollback()
                log.debug("job_deduped", type=type, dedupe_key=dedupe_key)
                return None
        await self._emit("job.queued", job)
        self._wake_queue(spec.queue)
        return job

    async def get(self, job_id: str) -> Job | None:
        async with self.db.sessionmaker() as s:
            return await s.get(Job, job_id)

    async def cancel(self, job_id: str) -> Job | None:
        async with self.db.sessionmaker() as s:
            job = await s.get(Job, job_id)
            if job is None or job.status in FINISHED:
                return job
            if job.status == "pending":
                job.status, job.finished_at = "cancelled", utcnow()
            else:
                job.cancel_requested = True
            await s.commit()
        if job.status == "cancelled":
            await self._emit("job.cancelled", job)
        elif (task := self._running.get(job_id)) is not None:
            task.cancel()
        return job

    async def retry(self, job_id: str) -> Job | None:
        """Manually re-run a failed or cancelled job from scratch."""
        async with self.db.sessionmaker() as s:
            job = await s.get(Job, job_id)
            if job is None or job.status not in ("failed", "cancelled"):
                return job
            job.status, job.attempts, job.error = "pending", 0, None
            job.cancel_requested, job.finished_at, job.run_after = False, None, utcnow()
            try:
                await s.commit()
            except IntegrityError:  # an identical job is already active
                await s.rollback()
                return None
        await self._emit("job.queued", job)
        self._wake_queue(job.queue)
        return job

    # ---- lifecycle ---------------------------------------------------------------

    async def recover(self) -> int:
        """Return orphaned running jobs to pending. Call once at startup."""
        async with self.db.sessionmaker() as s:
            res = await s.execute(
                update(Job)
                .where(Job.status == "running", Job.locked_by != self.worker_id)
                .values(status="pending", locked_by=None, locked_until=None, run_after=utcnow())
                .returning(Job.id)
            )
            ids = list(res.scalars())
            await s.commit()
        if ids:
            log.info("jobs_recovered", count=len(ids))
        return len(ids)

    async def start(self) -> None:
        await self.recover()
        self._stopping = False
        for queue, size in self.pools.items():
            self._wake[queue] = asyncio.Event()
            for n in range(size):
                self._workers.append(
                    asyncio.create_task(self._worker(queue), name=f"job-{queue}-{n}")
                )

    async def stop(self, timeout: float = 30.0) -> None:  # noqa: ASYNC109 - grace period
        """Stop claiming; let running jobs finish up to `timeout`, then checkpoint them."""
        self._stopping = True
        for ev in self._wake.values():
            ev.set()
        running = list(self._running.values())
        if running:
            _done, pending = await asyncio.wait(running, timeout=timeout)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        # Workers exit their loop on their own once _stopping is set; let them finish
        # writing checkpoints instead of cancelling them mid-write.
        if self._workers:
            _done, stuck = await asyncio.wait(self._workers, timeout=5.0)
            for w in stuck:
                w.cancel()
            await asyncio.gather(*stuck, return_exceptions=True)
        self._workers.clear()

    # ---- internals ---------------------------------------------------------------

    def _wake_queue(self, queue: str) -> None:
        if (ev := self._wake.get(queue)) is not None:
            ev.set()

    async def _worker(self, queue: str) -> None:
        wake = self._wake[queue]
        while not self._stopping:
            try:
                job = await self._claim(queue)
            except Exception:
                log.exception("job_claim_failed", queue=queue)
                job = None
            if job is None:
                wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=self.poll_interval)
                continue
            await self._execute(job)

    async def _claim(self, queue: str) -> Job | None:
        now = utcnow()
        async with self.db.sessionmaker() as s:
            candidate = (
                select(Job.id)
                .where(Job.queue == queue, Job.status == "pending", Job.run_after <= now)
                .order_by(Job.run_after, Job.id)
                .limit(1)
                .scalar_subquery()
            )
            res = await s.execute(
                update(Job)
                # status re-checked so two claimers racing on Postgres can't both win
                .where(Job.id == candidate, Job.status == "pending")
                .values(
                    status="running",
                    locked_by=self.worker_id,
                    locked_until=now + self.lease,
                    attempts=Job.attempts + 1,
                    started_at=now,
                )
                .returning(Job)
            )
            job = res.scalar_one_or_none()
            await s.commit()
            return job

    async def _execute(self, job: Job) -> None:
        spec = self._handlers.get(job.type)
        if spec is None:
            await self._finish(job, "failed", error=f"no handler for job type {job.type!r}")
            return
        await self._emit("job.started", job)
        ctx = JobContext(job.id, job.type, job.payload, job.attempts, self)
        task: asyncio.Task[dict[str, Any] | None] = asyncio.create_task(
            spec.fn(ctx), name=f"job:{job.type}:{job.id}"
        )
        self._running[job.id] = task
        heartbeat = asyncio.create_task(self._heartbeat(job.id, task))
        try:
            result = await task
        except (JobCancelled, asyncio.CancelledError):
            if self._stopping and not await self._is_cancel_requested(job.id):
                await self._checkpoint(job)
            else:
                await self._finish(job, "cancelled")
        except PermanentJobError as exc:
            await self._finish(job, "failed", error=str(exc) or type(exc).__name__)
        except Exception as exc:
            log.exception("job_failed", job_id=job.id, type=job.type, attempt=job.attempts)
            await self._fail_or_retry(job, f"{type(exc).__name__}: {exc}")
        else:
            await self._finish(job, "succeeded", result=result)
        finally:
            heartbeat.cancel()
            self._running.pop(job.id, None)
            self._last_progress_emit.pop(job.id, None)

    async def _heartbeat(self, job_id: str, task: asyncio.Task[Any]) -> None:
        interval = max(self.lease.total_seconds() / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            async with self.db.sessionmaker() as s:
                row = await s.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.locked_by == self.worker_id)
                    .values(locked_until=utcnow() + self.lease)
                    .returning(Job.cancel_requested)
                )
                cancel = row.scalar_one_or_none()
                await s.commit()
            if cancel:
                task.cancel()
                return

    async def _fail_or_retry(self, job: Job, error: str) -> None:
        if job.attempts >= job.max_attempts:
            await self._finish(job, "failed", error=error)
            return
        delay = backoff_delay(job.attempts)
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(
                    status="pending",
                    error=error,
                    locked_by=None,
                    locked_until=None,
                    run_after=utcnow() + timedelta(seconds=delay),
                )
            )
            await s.commit()
        log.info(
            "job_retry_scheduled",
            job_id=job.id,
            type=job.type,
            attempt=job.attempts,
            delay_s=round(delay, 1),
        )

    async def _checkpoint(self, job: Job) -> None:
        """Interrupted by shutdown: put it back without consuming an attempt."""
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(
                    status="pending",
                    locked_by=None,
                    locked_until=None,
                    attempts=Job.attempts - 1,
                    run_after=utcnow(),
                )
            )
            await s.commit()
        log.info("job_checkpointed", job_id=job.id, type=job.type)

    async def _finish(
        self,
        job: Job,
        status: JobStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "finished_at": utcnow(),
            "locked_by": None,
            "locked_until": None,
        }
        if status == "succeeded":
            values |= {"result": result, "progress": 1.0, "error": None}
        if error is not None:
            values["error"] = error
        async with self.db.sessionmaker() as s:
            await s.execute(update(Job).where(Job.id == job.id).values(**values))
            await s.commit()
            fresh = await s.get(Job, job.id, populate_existing=True)
        if fresh is not None:
            await self._emit(f"job.{status}", fresh)

    async def _report_progress(
        self, job_id: str, type: str, value: float, message: str | None
    ) -> None:
        value = min(max(value, 0.0), 1.0)
        async with self.db.sessionmaker() as s:
            await s.execute(update(Job).where(Job.id == job_id).values(progress=value))
            await s.commit()
        loop_now = asyncio.get_running_loop().time()
        last = self._last_progress_emit.get(job_id, 0.0)
        if value >= 1.0 or loop_now - last >= PROGRESS_EVENT_MIN_INTERVAL_S:
            self._last_progress_emit[job_id] = loop_now
            await self.bus.emit(
                "job.progress",
                subject=Subject(type="job", id=job_id),
                actor=Actor(type="job", id=job_id, name=type),
                job_type=type,
                progress=value,
                message=message,
            )

    async def _is_cancel_requested(self, job_id: str) -> bool:
        async with self.db.sessionmaker() as s:
            res = await s.execute(select(Job.cancel_requested).where(Job.id == job_id))
            return bool(res.scalar_one_or_none())

    async def _emit(self, event_type: str, job: Job) -> None:
        await self.bus.emit(
            event_type,
            subject=Subject(type="job", id=job.id),
            actor=Actor(type="system"),
            job_type=job.type,
            status=job.status,
            attempt=job.attempts,
            parent_id=job.parent_id,
            error=job.error,
        )
