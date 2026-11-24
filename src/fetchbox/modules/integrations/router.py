from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field, computed_field

from fetchbox.core.concurrency import etag
from fetchbox.core.errors import problem_responses as pr
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.integrations.models import MediaServer
from fetchbox.modules.integrations.service import IntegrationsService, PathMapping
from fetchbox.providers.media_servers import Library

router = APIRouter(prefix="/media-servers", tags=["integrations"])
_read = [Depends(require("integrations:read"))]
_write = [Depends(require("integrations:write"))]
IfMatch = Annotated[str | None, Header(description="ETag from a previous read; 412 if stale")]


def _svc(request: Request) -> IntegrationsService:
    svc: IntegrationsService = request.app.state.integrations
    return svc


class MediaServerOut(BaseModel):
    id: str
    kind: str
    name: str
    enabled: bool
    settings: dict[str, Any] = Field(description="Secrets come back masked")
    path_mappings: list[PathMapping]
    last_refresh_at: datetime | None
    last_error: str | None = Field(description="The last refresh's problem, if any")
    version: int = Field(exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def etag(self) -> str:
        return etag(self.version)


class MediaServerIn(BaseModel):
    kind: str = Field(description="plex, jellyfin or emby")
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    settings: dict[str, Any]
    path_mappings: list[PathMapping] = []


class MediaServerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    settings: dict[str, Any] | None = Field(
        default=None, description="Send the mask back to keep a secret"
    )
    path_mappings: list[PathMapping] | None = None


class CoverageOut(BaseModel):
    folder: str = Field(description="A Fetchbox library folder")
    local: str
    remote: str = Field(description="The same folder as the server sees it")
    library: str | None = Field(description="The server's library holding it; null: none")
    found: bool = Field(
        False, description="The server's path was worked out from its libraries, not a mapping"
    )


class MediaServerTestOut(BaseModel):
    ok: bool
    message: str
    libraries: list[Library]
    coverage: list[CoverageOut]


def _out(svc: IntegrationsService, row: MediaServer) -> MediaServerOut:
    return MediaServerOut(
        id=row.id,
        kind=row.kind,
        name=row.name,
        enabled=row.enabled,
        settings=svc.view(row),
        path_mappings=[PathMapping.model_validate(m) for m in row.path_mappings or []],
        last_refresh_at=row.last_refresh_at,
        last_error=row.last_error,
        version=row.version,
    )


@router.get("", response_model=list[MediaServerOut], dependencies=_read)
async def list_servers(request: Request) -> list[MediaServerOut]:
    svc = _svc(request)
    return [_out(svc, r) for r in await svc.all()]


@router.get("/kinds", response_model=list[str], dependencies=_read)
async def server_kinds(request: Request) -> list[str]:
    return _svc(request).kinds()


@router.post(
    "", response_model=MediaServerOut, status_code=201, dependencies=_write, responses=pr(409, 422)
)
async def add_server(
    body: MediaServerIn, request: Request, response: Response, p: CurrentPrincipal
) -> MediaServerOut:
    """Plex, Jellyfin or Emby to tell about new and changed files."""
    svc = _svc(request)
    row = await svc.create(body.model_dump(mode="json"), actor=p.actor())
    response.headers["Location"] = f"{request.url.path}/{row.id}"
    return _out(svc, row)


@router.patch(
    "/{server_id}",
    response_model=MediaServerOut,
    dependencies=_write,
    responses=pr(404, 409, 412, 422),
)
async def update_server(
    server_id: str,
    body: MediaServerPatch,
    request: Request,
    p: CurrentPrincipal,
    if_match: IfMatch = None,
) -> MediaServerOut:
    svc = _svc(request)
    changes = body.model_dump(mode="json", exclude_unset=True)
    row = await svc.update(server_id, changes, if_match=if_match, actor=p.actor())
    return _out(svc, row)


@router.delete("/{server_id}", status_code=204, dependencies=_write, responses=pr(404, 412))
async def remove_server(
    server_id: str, request: Request, p: CurrentPrincipal, if_match: IfMatch = None
) -> Response:
    await _svc(request).delete(server_id, if_match=if_match, actor=p.actor())
    return Response(status_code=204)


@router.post(
    "/{server_id}/test", response_model=MediaServerTestOut, dependencies=_read, responses=pr(404)
)
async def test_server(server_id: str, request: Request) -> MediaServerTestOut:
    """Connect, list the server's libraries, and show which one holds each Fetchbox
    library folder (after path mappings)."""
    dests = await request.app.state.storage.destinations()
    folders = [(d.name, d.path) for d in dests]
    return MediaServerTestOut.model_validate(await _svc(request).test(server_id, folders))
