"""Job handlers owned by the system module."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from sqlalchemy import delete

from fetchbox.core import idempotency
from fetchbox.core.db import utcnow
from fetchbox.core.jobs import FINISHED, Job, JobContext
from fetchbox.core.schedules import ScheduleDef
from fetchbox.core.state import AppState
from fetchbox.modules.auth.models import AuthSession
from fetchbox.modules.system import activity, service

SELFTEST = "system.selftest"
ACTIVITY_PRUNE = "system.activity_prune"
HOUSEKEEPING = "system.housekeeping"
HEALTH_CHECK = "system.health_check"
FINISHED_JOB_RETENTION = timedelta(days=30)


def register(state: AppState) -> None:
    runner = state.jobs

    @runner.handler(SELFTEST, queue="maintenance", max_attempts=3)
    async def selftest(ctx: JobContext) -> dict[str, Any]:
        """Diagnostic: sleeps in steps, reporting progress and honouring cancellation.

        Proves the runner, leases, recovery and event stream work on this install.
        """
        seconds = float(ctx.payload.get("seconds", 5))
        steps = max(int(seconds * 10), 1)
        for i in range(steps):
            await ctx.check_cancelled()
            await asyncio.sleep(seconds / steps)
            await ctx.progress((i + 1) / steps)
        return {"seconds": seconds, "attempt": ctx.attempt}

    @runner.handler(ACTIVITY_PRUNE, queue="maintenance")
    async def prune(ctx: JobContext) -> dict[str, Any]:
        days = int(ctx.payload.get("days", 90))
        removed = await activity.prune(state.db, timedelta(days=days))
        return {"removed": removed}

    @runner.handler(HOUSEKEEPING, queue="maintenance")
    async def housekeeping(ctx: JobContext) -> dict[str, Any]:
        """Nightly: prune activity (90 d), idempotency records (24 h), expired UI sessions,
        and finished jobs (30 d)."""
        now = utcnow()
        async with state.db.sessionmaker() as s:
            sessions = await s.execute(delete(AuthSession).where(AuthSession.expires_at < now))
            old_jobs = await s.execute(
                delete(Job).where(
                    Job.status.in_(FINISHED), Job.finished_at < now - FINISHED_JOB_RETENTION
                )
            )
            await s.commit()
        return {
            "activity": await activity.prune(state.db),
            "idempotency": await idempotency.prune(state.db),
            "sessions": int(sessions.rowcount or 0),  # type: ignore[attr-defined]
            "jobs": int(old_jobs.rowcount or 0),  # type: ignore[attr-defined]
        }

    @runner.handler(HEALTH_CHECK, queue="maintenance", max_attempts=1)
    async def health_check(ctx: JobContext) -> dict[str, Any]:
        """Runs all health checks; emits system.health_changed when the overall status moves."""
        report = await service.health_report(state)
        return {"status": report.status}

    sched = state.scheduler
    sched.define(
        ScheduleDef(
            "health_check",
            HEALTH_CHECK,
            timedelta(minutes=1),
            "Run all health checks",
            run_at_start=True,
        )
    )
    sched.define(
        ScheduleDef(
            "housekeeping",
            HOUSEKEEPING,
            timedelta(days=1),
            "Prune old activity, jobs, sessions and idempotency records",
        )
    )
