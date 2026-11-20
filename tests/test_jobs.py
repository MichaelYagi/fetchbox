from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from fetchbox import migrations
from fetchbox.core import jobs as jobs_mod
from fetchbox.core.db import Database, utcnow
from fetchbox.core.events import EventBus
from fetchbox.core.ids import new_id
from fetchbox.core.jobs import Job, JobContext, JobRunner, PermanentJobError

REAL_BACKOFF = jobs_mod.backoff_delay  # captured before the autouse patch


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}"
    await asyncio.to_thread(migrations.upgrade, url)
    d = Database(url)
    yield d
    await d.dispose()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def make_runner(db: Database, bus: EventBus) -> Callable[..., JobRunner]:
    def make(**kw: Any) -> JobRunner:
        kw.setdefault("pools", {"io": 2, "net": 1})
        kw.setdefault("lease", timedelta(seconds=0.3))
        kw.setdefault("poll_interval", 0.05)
        return JobRunner(db, bus, **kw)

    return make


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs_mod, "backoff_delay", lambda attempt: 0.0)


async def wait_status(runner: JobRunner, job_id: str, *statuses: str, within: float = 5) -> Job:
    async def poll() -> Job:
        while True:
            job = await runner.get(job_id)
            if job is not None and job.status in statuses:
                return job
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(poll(), within)


async def test_runs_job_and_emits_lifecycle(
    make_runner: Callable[..., JobRunner], bus: EventBus
) -> None:
    r = make_runner()

    @r.handler("echo", queue="net")
    async def echo(ctx: JobContext) -> dict[str, Any]:
        await ctx.progress(0.5)
        return {"got": ctx.payload["x"]}

    sub = bus.subscribe("job.*")
    await r.start()
    try:
        job = await r.enqueue("echo", {"x": 42})
        assert job is not None
        done = await wait_status(r, job.id, "succeeded")
    finally:
        await r.stop()
    assert done.result == {"got": 42}
    assert done.attempts == 1 and done.progress == 1.0 and done.finished_at is not None
    assert done.run_after.tzinfo is not None
    types = []
    while not sub.queue.empty():
        types.append(sub.queue.get_nowait().type)
    assert types == ["job.queued", "job.started", "job.progress", "job.succeeded"]


async def test_dedupe_only_while_active(make_runner: Callable[..., JobRunner]) -> None:
    r = make_runner()
    gate = asyncio.Event()

    @r.handler("search")
    async def search(ctx: JobContext) -> None:
        await gate.wait()

    first = await r.enqueue("search", dedupe_key="search:wanted:1")
    assert first is not None
    assert await r.enqueue("search", dedupe_key="search:wanted:1") is None
    assert await r.enqueue("search", dedupe_key="search:wanted:2") is not None
    await r.start()
    try:
        await wait_status(r, first.id, "running")
        assert await r.enqueue("search", dedupe_key="search:wanted:1") is None
        gate.set()
        await wait_status(r, first.id, "succeeded")
        assert await r.enqueue("search", dedupe_key="search:wanted:1") is not None
    finally:
        await r.stop()


async def test_retries_then_succeeds(make_runner: Callable[..., JobRunner]) -> None:
    r = make_runner()
    calls: list[int] = []

    @r.handler("flaky", max_attempts=3)
    async def flaky(ctx: JobContext) -> dict[str, int]:
        calls.append(ctx.attempt)
        if ctx.attempt < 3:
            raise RuntimeError("transient")
        return {"attempt": ctx.attempt}

    await r.start()
    try:
        job = await r.enqueue("flaky")
        assert job is not None
        done = await wait_status(r, job.id, "succeeded")
    finally:
        await r.stop()
    assert calls == [1, 2, 3]
    assert done.attempts == 3 and done.error is None


async def test_exhausts_attempts(make_runner: Callable[..., JobRunner], bus: EventBus) -> None:
    r = make_runner()

    @r.handler("broken", max_attempts=2)
    async def broken(ctx: JobContext) -> None:
        raise ValueError("nope")

    sub = bus.subscribe("job.failed")
    await r.start()
    try:
        job = await r.enqueue("broken")
        assert job is not None
        done = await wait_status(r, job.id, "failed")
    finally:
        await r.stop()
    assert done.attempts == 2
    assert done.error == "ValueError: nope"
    assert sub.queue.get_nowait().data["error"] == "ValueError: nope"


async def test_permanent_error_skips_retry(make_runner: Callable[..., JobRunner]) -> None:
    r = make_runner()

    @r.handler("bad_input", max_attempts=5)
    async def bad(ctx: JobContext) -> None:
        raise PermanentJobError("title not found")

    await r.start()
    try:
        job = await r.enqueue("bad_input")
        assert job is not None
        done = await wait_status(r, job.id, "failed")
    finally:
        await r.stop()
    assert done.attempts == 1 and done.error == "title not found"


async def test_future_run_after_not_claimed(make_runner: Callable[..., JobRunner]) -> None:
    r = make_runner()

    @r.handler("later")
    async def later(ctx: JobContext) -> None:
        return None

    await r.start()
    try:
        job = await r.enqueue("later", run_after=utcnow() + timedelta(hours=1))
        assert job is not None
        await asyncio.sleep(0.3)
        assert (await r.get(job.id)).status == "pending"  # type: ignore[union-attr]
    finally:
        await r.stop()


async def test_cancel_pending_and_running(make_runner: Callable[..., JobRunner]) -> None:
    r = make_runner(pools={"io": 1})
    started = asyncio.Event()

    @r.handler("long")
    async def long(ctx: JobContext) -> None:
        started.set()
        while True:
            await ctx.check_cancelled()
            await asyncio.sleep(0.02)

    running = await r.enqueue("long")
    queued = await r.enqueue("long")
    assert running is not None and queued is not None
    await r.start()
    try:
        await asyncio.wait_for(started.wait(), 5)
        await r.cancel(queued.id)
        assert (await r.get(queued.id)).status == "cancelled"  # type: ignore[union-attr]
        await r.cancel(running.id)
        await wait_status(r, running.id, "cancelled")
    finally:
        await r.stop()


async def test_cancel_reaches_handler_that_never_checks(
    make_runner: Callable[..., JobRunner],
) -> None:
    r = make_runner()

    @r.handler("stubborn")
    async def stubborn(ctx: JobContext) -> None:
        await asyncio.sleep(60)

    await r.start()
    try:
        job = await r.enqueue("stubborn")
        assert job is not None
        await wait_status(r, job.id, "running")
        await r.cancel(job.id)
        await wait_status(r, job.id, "cancelled", within=2)
    finally:
        await r.stop()


async def test_graceful_stop_checkpoints_without_burning_attempt(
    make_runner: Callable[..., JobRunner],
) -> None:
    r = make_runner()

    @r.handler("slow")
    async def slow(ctx: JobContext) -> None:
        await asyncio.sleep(60)

    await r.start()
    job = await r.enqueue("slow")
    assert job is not None
    await wait_status(r, job.id, "running")
    await r.stop(timeout=0.1)
    after = await r.get(job.id)
    assert after is not None
    assert after.status == "pending" and after.attempts == 0 and after.locked_by is None


async def test_recovers_job_orphaned_by_crash(
    db: Database, make_runner: Callable[..., JobRunner]
) -> None:
    """Phase 0 gate (in-process): a job left 'running' by a killed process resumes."""
    orphan_id = new_id()
    async with db.sessionmaker() as s:
        s.add(
            Job(
                id=orphan_id,
                type="import",
                queue="io",
                status="running",
                payload={"n": 1},
                attempts=1,
                max_attempts=3,
                locked_by="w-dead-process",
                locked_until=utcnow() + timedelta(minutes=5),
                cancel_requested=False,
                run_after=utcnow(),
                created_at=utcnow(),
                started_at=utcnow(),
            )
        )
        await s.commit()

    r = make_runner()

    @r.handler("import")
    async def do_import(ctx: JobContext) -> dict[str, int]:
        return {"attempt": ctx.attempt}

    await r.start()
    try:
        done = await wait_status(r, orphan_id, "succeeded")
    finally:
        await r.stop()
    assert done.result == {"attempt": 2}


async def test_child_jobs_link_to_parent(make_runner: Callable[..., JobRunner]) -> None:
    r = make_runner()
    child_ids: list[str] = []

    @r.handler("season_search")
    async def season(ctx: JobContext) -> None:
        for ep in range(3):
            child = await ctx.enqueue_child("episode_search", {"ep": ep})
            assert child is not None
            child_ids.append(child.id)

    @r.handler("episode_search", queue="net")
    async def episode(ctx: JobContext) -> None:
        return None

    await r.start()
    try:
        parent = await r.enqueue("season_search")
        assert parent is not None
        await wait_status(r, parent.id, "succeeded")
        for cid in child_ids:
            child = await wait_status(r, cid, "succeeded")
            assert child.parent_id == parent.id
    finally:
        await r.stop()


async def test_lease_renewed_while_running(make_runner: Callable[..., JobRunner]) -> None:
    r = make_runner(lease=timedelta(seconds=0.15))

    @r.handler("medium")
    async def medium(ctx: JobContext) -> None:
        await asyncio.sleep(0.6)  # 4x the lease

    await r.start()
    try:
        job = await r.enqueue("medium")
        assert job is not None
        running = await wait_status(r, job.id, "running")
        first_lease = running.locked_until
        await asyncio.sleep(0.3)
        renewed = await r.get(job.id)
        assert renewed is not None and renewed.status == "running"
        assert renewed.locked_until is not None and first_lease is not None
        assert renewed.locked_until > first_lease
        await wait_status(r, job.id, "succeeded")
    finally:
        await r.stop()


def test_registration_errors(make_runner: Callable[..., JobRunner]) -> None:
    r = make_runner()

    async def h(ctx: JobContext) -> None:
        return None

    r.register("a", h)
    with pytest.raises(ValueError):
        r.register("a", h)
    with pytest.raises(ValueError):
        r.register("b", h, queue="nope")


async def test_enqueue_unknown_type(make_runner: Callable[..., JobRunner]) -> None:
    with pytest.raises(LookupError):
        await make_runner().enqueue("nope")


def test_backoff_grows_and_caps() -> None:
    assert 8 <= REAL_BACKOFF(1) <= 12
    assert 16 <= REAL_BACKOFF(2) <= 24
    assert REAL_BACKOFF(30) <= jobs_mod.BACKOFF_MAX_S * 1.2
