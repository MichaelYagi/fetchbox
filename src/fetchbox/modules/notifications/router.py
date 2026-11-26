from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field, computed_field

from fetchbox.core.concurrency import etag
from fetchbox.core.errors import problem_responses as pr
from fetchbox.core.providers import TestResult
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.notifications.models import NotificationTarget
from fetchbox.modules.notifications.service import NotificationsService

router = APIRouter(prefix="/notifications", tags=["notifications"])
_read = [Depends(require("notifications:read"))]
_write = [Depends(require("notifications:write"))]
IfMatch = Annotated[str | None, Header(description="ETag from a previous read; 412 if stale")]


def _svc(request: Request) -> NotificationsService:
    svc: NotificationsService = request.app.state.notifications
    return svc


class TargetOut(BaseModel):
    id: str
    kind: str
    name: str
    enabled: bool
    settings: dict[str, Any] = Field(description="Secrets come back masked")
    events: list[str]
    last_sent_at: datetime | None
    last_error: str | None
    version: int = Field(exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def etag(self) -> str:
        return etag(self.version)


class TargetIn(BaseModel):
    kind: str = Field(description="webhook or discord")
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    settings: dict[str, Any]
    events: list[str] | None = Field(default=None, description="Null: the default set")


class TargetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    settings: dict[str, Any] | None = None
    events: list[str] | None = None


class EventKindOut(BaseModel):
    event: str
    label: str
    default: bool


def _out(svc: NotificationsService, row: NotificationTarget) -> TargetOut:
    return TargetOut(
        id=row.id, kind=row.kind, name=row.name, enabled=row.enabled, settings=svc.view(row),
        events=row.events or [], last_sent_at=row.last_sent_at, last_error=row.last_error,
        version=row.version,
    )  # fmt: skip


@router.get("/events", response_model=list[EventKindOut], dependencies=_read)
async def event_kinds(request: Request) -> list[EventKindOut]:
    """What can be notified about, with the default selection (plugin messages too while a
    plugin is running)."""
    return [
        EventKindOut(event=k, label=label, default=d) for k, label, d in _svc(request).event_kinds()
    ]


@router.get("/kinds", response_model=list[str], dependencies=_read)
async def target_kinds(request: Request) -> list[str]:
    return _svc(request).kinds()


@router.get("/targets", response_model=list[TargetOut], dependencies=_read)
async def list_targets(request: Request) -> list[TargetOut]:
    svc = _svc(request)
    return [_out(svc, r) for r in await svc.all()]


@router.post(
    "/targets", response_model=TargetOut, status_code=201, dependencies=_write,
    responses=pr(409, 422),
)  # fmt: skip
async def add_target(
    body: TargetIn, request: Request, response: Response, p: CurrentPrincipal
) -> TargetOut:
    svc = _svc(request)
    row = await svc.create(body.model_dump(mode="json"), actor=p.actor())
    response.headers["Location"] = f"{request.url.path}/{row.id}"
    return _out(svc, row)


@router.patch(
    "/targets/{target_id}", response_model=TargetOut, dependencies=_write,
    responses=pr(404, 409, 412, 422),
)  # fmt: skip
async def update_target(
    target_id: str, body: TargetPatch, request: Request, p: CurrentPrincipal,
    if_match: IfMatch = None,
) -> TargetOut:  # fmt: skip
    svc = _svc(request)
    changes = body.model_dump(mode="json", exclude_unset=True)
    return _out(svc, await svc.update(target_id, changes, if_match=if_match, actor=p.actor()))


@router.delete("/targets/{target_id}", status_code=204, dependencies=_write, responses=pr(404, 412))
async def remove_target(
    target_id: str, request: Request, p: CurrentPrincipal, if_match: IfMatch = None
) -> Response:
    await _svc(request).delete(target_id, if_match=if_match, actor=p.actor())
    return Response(status_code=204)


@router.post(
    "/targets/{target_id}/test", response_model=TestResult, dependencies=_write,
    responses=pr(404),
)  # fmt: skip
async def test_target(target_id: str, request: Request) -> TestResult:
    """Send a test notification now."""
    return await _svc(request).test(target_id)
