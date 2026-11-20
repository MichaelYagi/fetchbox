"""Health registry: every subsystem and provider contributes checks reporting ok|warning|error."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from fetchbox.core.providers import HealthLevel, HealthStatus

CHECK_TIMEOUT_S = 10.0

_RANK: dict[HealthLevel, int] = {"ok": 0, "warning": 1, "error": 2}


class CheckResult(BaseModel):
    name: str
    status: HealthLevel
    message: str = ""
    fix_url: str | None = None


class HealthReport(BaseModel):
    status: HealthLevel
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    checks: list[CheckResult]


Check = Callable[[], Awaitable[HealthStatus]]


class HealthRegistry:
    def __init__(self) -> None:
        self._checks: dict[str, tuple[Check, str | None]] = {}
        self.last: HealthReport | None = None

    def register(self, name: str, check: Check, *, fix_url: str | None = None) -> None:
        self._checks[name] = (check, fix_url)

    def unregister(self, name: str) -> None:
        self._checks.pop(name, None)

    async def _run_one(self, name: str, check: Check, fix_url: str | None) -> CheckResult:
        try:
            res = await asyncio.wait_for(check(), timeout=CHECK_TIMEOUT_S)
        except TimeoutError:
            res = HealthStatus(status="error", message=f"check timed out after {CHECK_TIMEOUT_S}s")
        except Exception as exc:
            res = HealthStatus(status="error", message=f"{type(exc).__name__}: {exc}")
        return CheckResult(
            name=name,
            status=res.status,
            message=res.message,
            fix_url=fix_url if res.status != "ok" else None,
        )

    async def run(self) -> tuple[HealthReport, bool]:
        """Run all checks. Returns (report, changed) where changed compares overall status."""
        results = await asyncio.gather(
            *(self._run_one(n, c, f) for n, (c, f) in sorted(self._checks.items()))
        )
        overall: HealthLevel = "ok"
        for r in results:
            if _RANK[r.status] > _RANK[overall]:
                overall = r.status
        report = HealthReport(status=overall, checks=list(results))
        changed = self.last is None or self.last.status != report.status
        self.last = report
        return report, changed
