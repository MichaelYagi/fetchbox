from __future__ import annotations

import platform
import sys
from datetime import UTC, datetime

from sqlalchemy import text

import fetchbox
from fetchbox import migrations
from fetchbox.core.health import HealthReport
from fetchbox.core.providers import HealthStatus
from fetchbox.core.state import AppState
from fetchbox.modules.system.schemas import StatusInfo, VersionInfo


def libtorrent_version() -> str | None:
    try:
        import libtorrent
    except ImportError:
        return None
    return str(libtorrent.__version__)


def version_info() -> VersionInfo:
    return VersionInfo(
        version=fetchbox.__version__,
        python=sys.version.split()[0],
        platform=f"{platform.system()} {platform.machine()}",
        libtorrent=libtorrent_version(),
    )


async def status_info(state: AppState) -> StatusInfo:
    now = datetime.now(UTC)
    return StatusInfo(
        version=fetchbox.__version__,
        started_at=state.started_at,
        uptime_seconds=(now - state.started_at).total_seconds(),
        database=state.db.engine.dialect.name,
        schema_revision=await migrations.current_revision(state.db.engine),
        schema_head=migrations.head_revision(),
        event_subscribers=state.bus.subscriber_count,
    )


async def health_report(state: AppState) -> HealthReport:
    report, changed = await state.health.run()
    if changed:
        await state.bus.emit("system.health_changed", status=report.status)
    return report


def register_core_checks(state: AppState) -> None:
    async def database() -> HealthStatus:
        async with state.db.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        current = await migrations.current_revision(state.db.engine)
        head = migrations.head_revision()
        if current != head:
            return HealthStatus(status="error", message=f"schema at {current}, expected {head}")
        return HealthStatus(status="ok", message=state.db.engine.dialect.name)

    async def torrent_engine() -> HealthStatus:
        v = libtorrent_version()
        if v is None:
            return HealthStatus(status="error", message="libtorrent bindings not importable")
        return HealthStatus(status="ok", message=f"libtorrent {v}")

    async def secrets_key() -> HealthStatus:
        path = state.data_dir / "secrets.key"
        if not path.exists():
            return HealthStatus(status="error", message="secrets.key missing; secrets unreadable")
        return HealthStatus(status="ok", message="present (keep it in your backups)")

    state.health.register("database", database, fix_url="/settings/system")
    state.health.register("secrets_key", secrets_key, fix_url="/settings/backups")
    state.health.register("torrent_engine", torrent_engine, fix_url="/settings/download-clients")
