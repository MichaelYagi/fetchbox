from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from fetchbox.core.health import HealthReport
from fetchbox.core.idempotency import IdempotencyKey, idempotent
from fetchbox.core.state import State
from fetchbox.modules.auth.deps import CurrentPrincipal, public, require
from fetchbox.modules.system import jobs as system_jobs
from fetchbox.modules.system import service
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
async def get_status(state: State) -> StatusInfo:
    return await service.status_info(state)


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
