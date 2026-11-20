from __future__ import annotations

from fastapi import APIRouter

from fetchbox.core.health import HealthReport
from fetchbox.core.state import State
from fetchbox.modules.system import service
from fetchbox.modules.system.schemas import StatusInfo, VersionInfo

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health", response_model=HealthReport)
async def get_health(state: State) -> HealthReport:
    return await service.health_report(state)


@router.get("/status", response_model=StatusInfo)
async def get_status(state: State) -> StatusInfo:
    return await service.status_info(state)


@router.get("/version", response_model=VersionInfo)
async def get_version() -> VersionInfo:
    return service.version_info()
