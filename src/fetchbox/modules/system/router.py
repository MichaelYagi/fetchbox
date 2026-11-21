from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field

from fetchbox.core.concurrency import etag
from fetchbox.core.errors import problem_responses
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
