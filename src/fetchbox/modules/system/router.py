from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field

from fetchbox.core.concurrency import etag
from fetchbox.core.errors import ProblemError, problem_responses
from fetchbox.core.health import HealthReport
from fetchbox.core.idempotency import IdempotencyKey, idempotent
from fetchbox.core.state import State
from fetchbox.modules.auth.deps import CurrentPrincipal, public, require
from fetchbox.modules.system import jobs as system_jobs
from fetchbox.modules.system import service
from fetchbox.modules.system.config import ConfigService
from fetchbox.modules.system.jobs_router import JobOut, accepted
from fetchbox.modules.system.schemas import StatusInfo, VersionInfo

router = APIRouter(prefix="/system", tags=["system"])


class Ping(BaseModel):
    status: str = "ok"


@router.get("/ping", response_model=Ping, dependencies=[Depends(public())])
async def ping() -> Ping:
    """Unauthenticated liveness probe for Docker/orchestrators. Reveals nothing else."""
    return Ping()


_read = [Depends(require("system:read"))]


@router.get("/health", response_model=HealthReport, dependencies=_read)
async def get_health(state: State) -> HealthReport:
    return await service.health_report(state)


@router.get("/status", response_model=StatusInfo, dependencies=_read)
async def get_status(state: State, request: Request) -> StatusInfo:
    """Includes `host`: the machine Fetchbox runs on; every path it shows is there."""
    return await service.status_info(state, request.app.state.downloads.downloads_root)


@router.get("/version", response_model=VersionInfo, dependencies=_read)
async def get_version() -> VersionInfo:
    return service.version_info()


class SelftestIn(BaseModel):
    seconds: float = Field(default=5, ge=0, le=300)


@router.post(
    "/selftest",
    status_code=202,
    response_model=JobOut,
    dependencies=[Depends(require("system:write"))],
)
async def selftest(
    body: SelftestIn,
    request: Request,
    state: State,
    p: CurrentPrincipal,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Queue a diagnostic job that reports progress; watch it on the event stream."""

    async def run() -> Response:
        job = await state.jobs.enqueue(system_jobs.SELFTEST, {"seconds": body.seconds})
        assert job is not None  # no dedupe key
        return accepted(job)

    return await idempotent(state.db, request, actor_id=p.id, key=idempotency_key, run=run)


# ---- runtime config -----------------------------------------------------------------------


class ConfigOut(BaseModel):
    config: dict[str, Any] = Field(description="Effective config; secrets are masked")
    overridden: list[str] = Field(description="Fields set by environment variables")


def _config(request: Request) -> ConfigService:
    svc: ConfigService = request.app.state.config
    return svc


def _config_out(view: dict[str, Any], response: Response) -> ConfigOut:
    response.headers["ETag"] = etag(view["version"])
    return ConfigOut(config=view["config"], overridden=view["overridden"])


@router.get("/config", response_model=ConfigOut, dependencies=_read)
async def get_config(request: Request, response: Response) -> ConfigOut:
    """Runtime settings. Secrets read back as `********`; send that back to keep one."""
    return _config_out(_config(request).view(), response)


@router.patch(
    "/config",
    response_model=ConfigOut,
    dependencies=[Depends(require("system:write"))],
    responses=problem_responses(409, 412, 422),
)
async def patch_config(
    body: dict[str, Any],
    request: Request,
    response: Response,
    p: CurrentPrincipal,
    if_match: Annotated[str | None, Header()] = None,
) -> ConfigOut:
    """Partial update, merged per section, e.g. `{"tmdb": {"api_key": "..."}}`."""
    view = await _config(request).update(body, if_match=if_match, actor=p.actor())
    return _config_out(view, response)


SUPERVISOR_ENV = "FETCHBOX_SUPERVISOR_TOKEN"


@router.post(
    "/shutdown",
    status_code=202,
    dependencies=[Depends(public())],
    responses=problem_responses(401, 403),
)
async def shutdown(
    request: Request,
    x_fetchbox_supervisor: Annotated[str | None, Header()] = None,
) -> Response:
    """Stop the server gracefully (running jobs get up to 30 s, downloads save their
    state). Needs ``system:write``, or the token the desktop tray gave the server it
    started (``X-Fetchbox-Supervisor``)."""
    expected = os.environ.get(SUPERVISOR_ENV)
    if not (
        expected
        and x_fetchbox_supervisor
        and hmac.compare_digest(expected.encode(), x_fetchbox_supervisor.encode())
    ):
        await require("system:write")(request)
    stop = getattr(request.app.state, "request_shutdown", None)
    if stop is None:
        raise ProblemError(409, "Conflict", "this server can't stop itself", slug="conflict")
    asyncio.get_running_loop().call_later(0.2, stop)  # after this response is sent
    return Response(status_code=202)


class NetworkInterface(BaseModel):
    name: str
    address: str | None = Field(description="Its current address; null: down or none")
    up: bool
    vpn: bool = Field(description="Looks like a VPN (tun, wg, utun, ProtonVPN, …)")


_VPN_NAMES = ("tun", "tap", "wg", "utun", "ppp", "ipsec", "proton", "nord", "mullvad",
              "wireguard", "vpn", "openvpn", "wintun", "tailscale", "zt")  # fmt: skip


@router.get("/network-interfaces", response_model=list[NetworkInterface], dependencies=_read)
async def network_interfaces() -> list[NetworkInterface]:
    """This computer's network interfaces, to bind the torrent engine to one (a VPN)."""

    def collect() -> list[NetworkInterface]:
        import psutil

        from fetchbox.providers.download_clients.builtin import interface_address

        out = []
        for name, st in psutil.net_if_stats().items():
            low = name.lower()
            vpn = low.startswith(_VPN_NAMES) or any(v in low for v in _VPN_NAMES[5:])
            out.append(
                NetworkInterface(name=name, address=interface_address(name), up=st.isup, vpn=vpn)
            )
        return sorted(out, key=lambda i: (not i.vpn, not i.up, i.name.lower()))

    return await asyncio.to_thread(collect)


_SECRETISH = re.compile(
    r"(?i)((?:api[_-]?key|apikey|token|password|passkey|secret|authorization)[\"'=:\s]+)"
    r"([^\s\"'&,;]{6,})"
)
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_FB_KEY = re.compile(r"\bfb_[A-Za-z0-9_-]{8,}")


def redact(text: str) -> str:
    """Scrub what looks like a credential: key=value pairs, long hex keys, Fetchbox keys."""
    text = _SECRETISH.sub(lambda m: m.group(1) + "[redacted]", text)
    text = _FB_KEY.sub("fb_[redacted]", text)
    return _LONG_HEX.sub("[redacted-hex]", text)


@router.get(
    "/diagnostics",
    dependencies=[Depends(require("system:read"))],
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}}},
)
async def diagnostics(request: Request, state: State) -> Response:
    """A zip to attach to a bug report: settings (secrets masked), recent logs (keys
    scrubbed), the health report, versions and a summary of indexers, clients and folders
    (kinds and statuses, no addresses or credentials)."""
    import io
    import platform
    import sqlite3
    import sys
    import zipfile
    from datetime import UTC, datetime

    import fetchbox
    from fetchbox import migrations

    app = request.app
    report = await service.health_report(state)
    try:
        import libtorrent

        lt_version = str(libtorrent.__version__)
    except ImportError:
        lt_version = "not installed"
    versions = {
        "fetchbox": fetchbox.__version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "libtorrent": lt_version,
        "sqlite": sqlite3.sqlite_version,
        "schema_revision": await migrations.current_revision(state.db.engine),
        "schema_head": migrations.head_revision(),
    }
    indexers = [
        {
            "kind": i.kind,
            "enabled": i.enabled,
            "failures": i.failure_count,
            "last_error": redact(i.last_error or ""),
        }
        for i in await app.state.sources.all()
    ]
    running = app.state.downloads.clients
    clients = [
        {"kind": c.kind, "enabled": c.enabled, "running": c.id in running}
        for c in await app.state.downloads.client_rows()
    ]
    folders = [
        {
            "kind": d.media_kind,
            "status": d.status,
            "message": d.status_message,
            "link_mode": d.link_mode,
            "mounted": d.mount_point is not None,
        }
        for d in await app.state.storage.destinations()
    ]
    logs = ""
    log_file = state.data_dir / "logs" / "fetchbox.log"
    if log_file.is_file():
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
        logs = redact("\n".join(lines))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("versions.json", json.dumps(versions, indent=2))
        z.writestr("health.json", report.model_dump_json(indent=2))
        z.writestr("config.json", json.dumps(_config(request).view(), indent=2, default=str))
        z.writestr("setup.json", json.dumps(
            {"indexers": indexers, "download_clients": clients, "library_folders": folders},
            indent=2))  # fmt: skip
        z.writestr("fetchbox.log", logs)
    name = f"fetchbox-diagnostics-{datetime.now(UTC):%Y%m%d-%H%M%S}.zip"
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})  # fmt: skip


class UpdateOut(BaseModel):
    current: str
    latest: str | None
    update_available: bool
    url: str | None = Field(description="The release page")
    published_at: str | None
    checked: bool = Field(description="False: checking is off, or GitHub couldn't be reached")
    error: str | None = None


@router.get("/updates", response_model=UpdateOut, dependencies=_read)
async def check_updates(request: Request, fresh: bool = False) -> UpdateOut:
    """Is a newer Fetchbox released? (Settings: updates.check; nothing is installed.)"""
    from dataclasses import asdict

    checker = request.app.state.updates
    info = await checker.check(enabled=_config(request).current.updates.check, fresh=fresh)
    return UpdateOut(**asdict(info))
