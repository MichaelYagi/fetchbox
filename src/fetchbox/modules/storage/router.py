from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field, computed_field, field_validator

from fetchbox.core.concurrency import etag
from fetchbox.core.errors import problem_responses as pr
from fetchbox.core.paths import host_path
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.storage.models import Destination, StorageNode
from fetchbox.modules.storage.service import StorageService, mounts

router = APIRouter(tags=["storage"])
_read = [Depends(require("storage:read"))]
_write = [Depends(require("storage:write"))]
IfMatch = Annotated[str | None, Header(description="ETag from a previous read; 412 if stale")]
LinkMode = Literal["hardlink_or_copy", "hardlink_only", "copy", "move", "symlink"]
Kind = Literal["movie", "series"]


def _svc(request: Request) -> StorageService:
    svc: StorageService = request.app.state.storage
    return svc


class _Versioned(BaseModel):
    version: int = Field(exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def etag(self) -> str:
        return etag(self.version)


class NodeOut(_Versioned):
    id: str
    kind: str
    name: str
    status: str
    created_at: datetime


class DestinationOut(_Versioned):
    id: str
    node_id: str
    name: str
    path: str
    media_kind: Kind
    naming_template_id: str | None
    link_mode: LinkMode
    free_space_min: int | None = Field(description="Bytes; null = 5% / 20 GB default")
    recycle: bool
    is_default: bool
    status: str = Field(
        description="online, degraded, low_space, offline, unmounted (its NAS share or "
        "drive is disconnected), unknown"
    )
    mount_point: str | None = Field(
        default=None, description="The share or drive it lives on, watched for unmounting"
    )
    status_message: str | None
    checked_at: datetime | None
    suggested_path: str | None = Field(
        default=None,
        description="When this folder means a second copy of every download: a folder on "
        "the downloads' drive to move it to (PATCH path, which moves its files)",
    )


class DestinationIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=4000, description="Absolute folder path")
    media_kind: Kind
    node_id: str | None = None
    naming_template_id: str | None = None
    link_mode: LinkMode = "hardlink_or_copy"
    free_space_min: int | None = Field(default=None, ge=0)
    recycle: bool = True
    is_default: bool = False

    @field_validator("path", mode="before")
    @classmethod
    def _host(cls, v: object) -> object:
        r"""Under WSL, a Windows path (C:\Media) is taken as its /mnt/c/Media form."""
        return host_path(v) if isinstance(v, str) else v


class DestinationPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    path: str | None = Field(default=None, min_length=1, max_length=4000)
    naming_template_id: str | None = None
    link_mode: LinkMode | None = None
    free_space_min: int | None = Field(default=None, ge=0)
    recycle: bool | None = None
    is_default: bool | None = None

    @field_validator("path", mode="before")
    @classmethod
    def _host(cls, v: object) -> object:
        r"""Under WSL, a Windows path (C:\Media) is taken as its /mnt/c/Media form."""
        return host_path(v) if isinstance(v, str) else v


def _node(n: StorageNode) -> NodeOut:
    return NodeOut(
        id=n.id,
        version=n.version,
        kind=n.kind,
        name=n.name,
        status=n.status,
        created_at=n.created_at,
    )


def _dest(d: Destination, response: Response | None = None) -> DestinationOut:
    out = DestinationOut.model_validate(d, from_attributes=True)
    if response is not None:
        response.headers["ETag"] = out.etag
    return out


@router.get("/storage/nodes", response_model=list[NodeOut], dependencies=_read)
async def list_nodes(request: Request) -> list[NodeOut]:
    return [_node(n) for n in await _svc(request).nodes()]


@router.get(
    "/storage/nodes/{node_id}", response_model=NodeOut, dependencies=_read, responses=pr(404)
)
async def get_node(node_id: str, request: Request) -> NodeOut:
    return _node(await _svc(request).node(node_id))


@router.get("/destinations", response_model=list[DestinationOut], dependencies=_read)
async def list_destinations(
    request: Request, media_kind: Kind | None = None
) -> list[DestinationOut]:
    """Default first. Status is from the last check (every minute, and on change)."""
    return [_dest(d) for d in await _svc(request).destinations(media_kind)]


@router.post(
    "/destinations",
    response_model=DestinationOut,
    status_code=201,
    dependencies=_write,
    responses=pr(409, 422),
)
async def add_destination(
    body: DestinationIn, request: Request, response: Response, p: CurrentPrincipal
) -> DestinationOut:
    d = await _svc(request).create_destination(body.model_dump(), actor=p.actor())
    response.headers["Location"] = f"{request.url.path}/{d.id}"
    return _dest(d, response)


@router.get(
    "/destinations/{dest_id}", response_model=DestinationOut, dependencies=_read, responses=pr(404)
)
async def get_destination(dest_id: str, request: Request, response: Response) -> DestinationOut:
    return _dest(await _svc(request).destination(dest_id), response)


@router.patch(
    "/destinations/{dest_id}",
    response_model=DestinationOut,
    dependencies=_write,
    responses=pr(404, 409, 412, 422),
)
async def update_destination(
    dest_id: str,
    body: DestinationPatch,
    request: Request,
    response: Response,
    p: CurrentPrincipal,
    if_match: IfMatch = None,
) -> DestinationOut:
    changes = body.model_dump(exclude_unset=True)
    d = await _svc(request).update_destination(dest_id, changes, if_match=if_match, actor=p.actor())
    return _dest(d, response)


@router.delete(
    "/destinations/{dest_id}", status_code=204, dependencies=_write, responses=pr(404, 412)
)
async def delete_destination(
    dest_id: str, request: Request, p: CurrentPrincipal, if_match: IfMatch = None
) -> Response:
    """Files in the folder are not touched."""
    await _svc(request).delete_destination(dest_id, if_match=if_match, actor=p.actor())
    return Response(status_code=204)


@router.post(
    "/destinations/{dest_id}/check",
    response_model=DestinationOut,
    dependencies=_write,
    responses=pr(404),
)
async def check_destination(dest_id: str, request: Request) -> DestinationOut:
    """Probe now: folder, write/delete, hardlinks from the torrent folder, free space."""
    return _dest(await _svc(request).check(dest_id))


class MountOut(BaseModel):
    path: str
    device: str
    fstype: str
    network: bool = Field(description="A NAS / network share")
    total: int
    free: int


@router.get("/storage/mounts", response_model=list[MountOut], dependencies=_read)
async def list_mounts() -> list[MountOut]:
    """Drives and network shares mounted on this computer: candidates for library folders."""
    return [MountOut(**m) for m in await asyncio.to_thread(mounts)]
