from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from fetchbox import migrations
from fetchbox.core.db import Database, utcnow
from fetchbox.core.events import EventBus
from fetchbox.core.jobs import JobContext, JobRunner
from fetchbox.core.schedules import Schedule, ScheduleDef, Scheduler


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[tuple[Database, JobRunner, asyncio.Event]]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 's.db').as_posix()}"
    await asyncio.to_thread(migrations.upgrade, url)
    db = Database(url)
    runner = JobRunner(db, EventBus(), pools={"maintenance": 1}, poll_interval=0.05)
    gate = asyncio.Event()

    @runner.handler("t.tick", queue="maintenance")
    async def tick(ctx: JobContext) -> None:
        await gate.wait()

    yield db, runner, gate
    gate.set()
    await runner.stop()
    await db.dispose()


async def _row(db: Database, name: str) -> Schedule:
    async with db.sessionmaker() as s:
        row = await s.get(Schedule, name)
    assert row is not None
    return row


async def test_sync_creates_rows_and_keeps_user_edits(
    env: tuple[Database, JobRunner, asyncio.Event],
) -> None:
    db, runner, _ = env
    sched = Scheduler(db, runner)
    sched.define(ScheduleDef("tick", "t.tick", timedelta(minutes=5), "d"))
    await sched.sync()
    row = await _row(db, "tick")
    assert row.interval_seconds == 300 and row.next_run_at > utcnow()

    async with db.sessionmaker() as s:
        r = await s.get(Schedule, "tick")
        assert r is not None
        r.interval_seconds, r.enabled = 900, False
        await s.commit()

    again = Scheduler(db, runner)  # e.g. after a restart, possibly with a new default
    again.define(ScheduleDef("tick", "t.tick", timedelta(minutes=10), "new description"))
    await again.sync()
    row = await _row(db, "tick")
    assert (row.interval_seconds, row.enabled) == (900, False)
    assert (row.default_interval_seconds, row.description) == (600, "new description")


async def test_due_schedule_fires_once_and_dedupes(
    env: tuple[Database, JobRunner, asyncio.Event],
) -> None:
    db, runner, _ = env
    sched = Scheduler(db, runner)
    sched.define(ScheduleDef("tick", "t.tick", timedelta(minutes=1), run_at_start=True))
    await sched.sync()
    assert await sched.run_due() == ["tick"]
    first = await _row(db, "tick")
    assert first.last_job_id and first.next_run_at > utcnow() + timedelta(seconds=50)
    assert first.version == 1  # bookkeeping doesn't change the ETag
    assert await sched.run_due() == []  # not due again yet

    # Manual run while the scheduled one is still pending: deduped.
    assert await sched.run_now("tick") is None
    with pytest.raises(LookupError):
        await sched.run_now("missing")


async def test_disabled_and_unknown_schedules_skip(
    env: tuple[Database, JobRunner, asyncio.Event],
) -> None:
    db, runner, _ = env
    sched = Scheduler(db, runner)
    sched.define(
        ScheduleDef("tick", "t.tick", timedelta(minutes=1), run_at_start=True, enabled=False)
    )
    await sched.sync()
    assert await sched.run_due() == []
    later = Scheduler(db, runner)  # module that defined "tick" is gone
    async with db.sessionmaker() as s:
        r = await s.get(Schedule, "tick")
        assert r is not None
        r.enabled = True
        await s.commit()
    assert await later.run_due() == []


def test_interval_bounds() -> None:
    sched = Scheduler.__new__(Scheduler)
    sched._defs = {}
    with pytest.raises(ValueError):
        sched.define(ScheduleDef("x", "t", timedelta(seconds=5)))
    with pytest.raises(ValueError):
        sched.define(ScheduleDef("x", "t", timedelta(days=365)))


# ---- API -------------------------------------------------------------------------------------


async def test_builtin_schedules_listed(client: httpx.AsyncClient) -> None:
    rows = {r["name"]: r for r in (await client.get("/automation/schedules")).json()}
    assert rows["health_check"]["interval_seconds"] == 60
    assert rows["housekeeping"]["interval_seconds"] == 86400
    assert rows["health_check"]["etag"] == '"v1"'


async def test_patch_schedule_with_etag(client: httpx.AsyncClient) -> None:
    r = await client.get("/automation/schedules/housekeeping")
    tag = r.headers["etag"]
    ok = await client.patch(
        "/automation/schedules/housekeeping",
        json={"interval_seconds": 3600},
        headers={"If-Match": tag},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["interval_seconds"] == 3600 and ok.headers["etag"] != tag
    stale = await client.patch(
        "/automation/schedules/housekeeping", json={"enabled": False}, headers={"If-Match": tag}
    )
    assert stale.status_code == 412
    bad = await client.patch("/automation/schedules/housekeeping", json={"interval_seconds": 1})
    assert bad.status_code == 422
    reset = await client.patch("/automation/schedules/housekeeping", json={"reset_interval": True})
    assert reset.json()["interval_seconds"] == 86400


async def test_run_housekeeping_now(client: httpx.AsyncClient) -> None:
    r = await client.post("/automation/schedules/housekeeping/run")
    assert r.status_code == 202
    job_id = r.json()["id"]
    for _ in range(200):
        job = (await client.get(f"/jobs/{job_id}")).json()
        if job["status"] == "succeeded":
            break
        await asyncio.sleep(0.02)
    assert set(job["result"]) == {"activity", "idempotency", "sessions", "jobs"}


async def test_schedule_scopes(server: object, client: httpx.AsyncClient) -> None:
    from tests.conftest import Running

    assert isinstance(server, Running)
    ro = (await client.post("/auth/keys", json={"name": "r", "scopes": ["automation:read"]})).json()
    async with server.client(ro["key"]) as c:
        assert (await c.get("/automation/schedules")).status_code == 200
        assert (await c.post("/automation/schedules/housekeeping/run")).status_code == 403
