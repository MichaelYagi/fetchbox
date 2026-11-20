from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from fetchbox.core.health import HealthReport
from fetchbox.core.state import State
from fetchbox.modules.auth.deps import public, require
from fetchbox.modules.system import service
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
