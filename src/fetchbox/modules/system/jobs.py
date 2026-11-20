"""Job handlers owned by the system module."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from fetchbox.core.jobs import JobContext
from fetchbox.core.state import AppState
from fetchbox.modules.system import activity

SELFTEST = "system.selftest"
ACTIVITY_PRUNE = "system.activity_prune"


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
