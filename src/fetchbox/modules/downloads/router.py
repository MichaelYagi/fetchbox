from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field, computed_field

from fetchbox.core.concurrency import etag
from fetchbox.core.errors import problem_responses as pr
from fetchbox.core.providers import TestResult
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.downloads.models import STATUSES, Download, DownloadClient
from fetchbox.modules.downloads.service import DownloadsService, Policy
from fetchbox.providers.download_clients import TransferLimits

router = APIRouter(tags=["downloads"])
_read = [Depends(require("downloads:read"))]
_write = [Depends(require("downloads:write"))]
IfMatch = Annotated[str | None, Header(description="ETag from a previous read; 412 if stale")]
Status = Literal["queued", "downloading", "paused", "completed", "failed", "removed"]
assert set(Status.__args__) == set(STATUSES)  # type: ignore[attr-defined]


def _svc(request: Request) -> DownloadsService:
    svc: DownloadsService = request.app.state.downloads
    return svc


class _Versioned(BaseModel):
    version: int = Field(exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def etag(self) -> str:
        return etag(self.version)


class LiveOut(BaseModel):
    state: str
    download_rate: int = Field(description="Bytes/s")
    upload_rate: int
    seeds: int
    peers: int
    eta_seconds: int | None
    downloaded: int
    uploaded: int


class DownloadOut(_Versioned):
    id: str
    grab_id: str
    client_id: str | None = Field(description="null once its client was removed")
    info_hash: str
    name: str
    media_kind: str
    status: Status
    queue_priority: int
    progress: float
    size: int | None
    save_path: str | None
    ratio: float | None
    seed_seconds: int | None
    error: str | None
    stalls: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    removed_at: datetime | None
    seeded_at: datetime | None = Field(
        description="When the seeding goal was met and the torrent was stopped"
    )
    live: LiveOut | None = Field(description="From the client's last poll (active downloads)")


def _out(svc: DownloadsService, d: Download, response: Response | None = None) -> DownloadOut:
    t = svc.live.get(d.id)
    out = DownloadOut(
        id=d.id,
        version=d.version,
        grab_id=d.grab_id,
        client_id=d.client_id,
        info_hash=d.client_ref,
        name=d.name,
        media_kind=d.media_kind,
        status=d.status,
        queue_priority=d.queue_priority,
        progress=d.progress,
        size=d.size,
        save_path=d.save_path,
        ratio=d.ratio,
        seed_seconds=d.seed_seconds,
        error=d.error,
        stalls=d.attempts,
        created_at=d.created_at,
        started_at=d.started_at,
        completed_at=d.completed_at,
        removed_at=d.removed_at,
        seeded_at=d.seeded_at,
        live=LiveOut(**t.model_dump(include=set(LiveOut.model_fields)))
        if t is not None and d.status in ("queued", "downloading", "paused", "completed")
        else None,
    )
    if response is not None:
        response.headers["ETag"] = out.etag
    return out


class Count(BaseModel):
    count: int


class ReorderIn(BaseModel):
    ids: list[str] = Field(min_length=1, description="Download ids, first runs first")


class PriorityPatch(BaseModel):
    queue_priority: int = Field(ge=-1000, le=1000, description="Higher runs first")


class LimitsIO(BaseModel):
    download: int | None = Field(default=None, ge=0, description="Bytes/s; null = unlimited")
    upload: int | None = Field(default=None, ge=0)


# ---- downloads ----------------------------------------------------------------------------


@router.get("/downloads", response_model=list[DownloadOut], dependencies=_read)
async def list_downloads(
    request: Request,
    status: Annotated[list[Status] | None, Query(description="Repeatable filter")] = None,
) -> list[DownloadOut]:
    """Queue order: highest priority first, then oldest."""
    svc = _svc(request)
    return [_out(svc, d) for d in await svc.downloads(status)]


@router.get("/downloads/limits", response_model=LimitsIO, dependencies=_read)
async def get_limits(request: Request) -> LimitsIO:
    lim = await _svc(request).limits()
    return LimitsIO(download=lim.download, upload=lim.upload)


@router.put("/downloads/limits", response_model=LimitsIO, dependencies=_write, responses=pr(422))
async def put_limits(body: LimitsIO, request: Request, p: CurrentPrincipal) -> LimitsIO:
    """Global speed limits for the built-in engine."""
    lim = await _svc(request).set_limits(
        TransferLimits(body.download, body.upload), actor=p.actor()
    )
    return LimitsIO(download=lim.download, upload=lim.upload)


@router.post(
    "/downloads/reorder", response_model=list[DownloadOut], dependencies=_write, responses=pr(422)
)
async def reorder(body: ReorderIn, request: Request) -> list[DownloadOut]:
    svc = _svc(request)
    return [_out(svc, d) for d in await svc.reorder(body.ids)]


@router.post("/downloads/pause-all", response_model=Count, dependencies=_write)
async def pause_all(request: Request, p: CurrentPrincipal) -> Count:
    return Count(count=await _svc(request).pause_all(actor=p.actor()))


@router.post("/downloads/resume-all", response_model=Count, dependencies=_write)
async def resume_all(request: Request, p: CurrentPrincipal) -> Count:
    return Count(count=await _svc(request).resume_all(actor=p.actor()))


@router.get(
    "/downloads/{download_id}", response_model=DownloadOut, dependencies=_read, responses=pr(404)
)
async def get_download(download_id: str, request: Request, response: Response) -> DownloadOut:
    svc = _svc(request)
    return _out(svc, await svc.get(download_id), response)


@router.patch(
    "/downloads/{download_id}",
    response_model=DownloadOut,
    dependencies=_write,
    responses=pr(404, 412, 422),
)
async def patch_download(
    download_id: str,
    body: PriorityPatch,
    request: Request,
    response: Response,
    p: CurrentPrincipal,
    if_match: IfMatch = None,
) -> DownloadOut:
    svc = _svc(request)
    d = await svc.set_priority(download_id, body.queue_priority, if_match=if_match, actor=p.actor())
    return _out(svc, d, response)


@router.delete("/downloads/{download_id}", status_code=204, dependencies=_write, responses=pr(404))
async def delete_download(
    download_id: str,
    request: Request,
    p: CurrentPrincipal,
    delete_data: bool = Query(False, description="Also delete the downloaded files"),
    blocklist: bool = Query(False, description="Never grab this release again"),
) -> Response:
    """Remove from the client. An unfinished download's items go back to `missing`."""
    await _svc(request).remove(
        download_id, delete_data=delete_data, blocklist=blocklist, actor=p.actor()
    )
    return Response(status_code=204)


@router.post(
    "/downloads/{download_id}/pause",
    response_model=DownloadOut,
    dependencies=_write,
    responses=pr(404, 409),
)
async def pause(download_id: str, request: Request, p: CurrentPrincipal) -> DownloadOut:
    svc = _svc(request)
    return _out(svc, await svc.pause(download_id, actor=p.actor()))


@router.post(
    "/downloads/{download_id}/resume",
    response_model=DownloadOut,
    dependencies=_write,
    responses=pr(404, 409),
)
async def resume(download_id: str, request: Request, p: CurrentPrincipal) -> DownloadOut:
    """Back into the queue; it starts when a slot is free."""
    svc = _svc(request)
    return _out(svc, await svc.resume(download_id, actor=p.actor()))


@router.post(
    "/downloads/{download_id}/recheck", status_code=202, dependencies=_write, responses=pr(404, 409)
)
async def recheck(download_id: str, request: Request) -> Response:
    """Verify the downloaded data against the torrent's hashes."""
    await _svc(request).recheck(download_id)
    return Response(status_code=202)


# ---- clients ------------------------------------------------------------------------------


class ClientOut(_Versioned):
    id: str
    kind: str
    name: str
    enabled: bool
    priority: int
    settings: dict[str, Any]
    policy: Policy
    media_kinds: list[str]
    tags: list[str]
    running: bool


class ClientPatch(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=1, le=50)
    settings: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    media_kinds: list[Literal["movie", "series"]] | None = None
    tags: list[str] | None = None


def _client_out(
    svc: DownloadsService, row: DownloadClient, response: Response | None = None
) -> ClientOut:
    out = ClientOut(
        id=row.id,
        version=row.version,
        kind=row.kind,
        name=row.name,
        enabled=row.enabled,
        priority=row.priority,
        settings=svc.masked(row),
        policy=svc.policy(row),
        media_kinds=row.media_kinds or [],
        tags=row.tags or [],
        running=row.id in svc.clients,
    )
    if response is not None:
        response.headers["ETag"] = out.etag
    return out


class ClientIn(BaseModel):
    kind: str = Field(
        description="qbittorrent, transmission or deluge (see /download-clients/kinds)"
    )
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    priority: int = Field(default=1, ge=1, le=50, description="Lower is tried first")
    settings: dict[str, Any] = {}
    policy: dict[str, Any] = {}
    media_kinds: list[Literal["movie", "series"]] = Field(default=[], description="Empty = all")
    tags: list[str] = []


class ClientKindOut(BaseModel):
    kind: str
    settings_schema: dict[str, Any] = Field(description="JSON Schema for the settings form")


@router.get("/download-clients/kinds", response_model=list[ClientKindOut], dependencies=_read)
async def list_client_kinds(request: Request) -> list[ClientKindOut]:
    """Client kinds this install can add, with the settings each one takes."""
    return [
        ClientKindOut(kind=c.kind, settings_schema=c.Settings.model_json_schema())
        for c in _svc(request).kinds()
        if c.kind not in ("builtin", "direct")  # built in, one of each
    ]


@router.post(
    "/download-clients",
    response_model=ClientOut,
    status_code=201,
    dependencies=_write,
    responses=pr(409, 422),
)
async def add_client(
    body: ClientIn, request: Request, response: Response, p: CurrentPrincipal
) -> ClientOut:
    """Add qBittorrent, Transmission or Deluge. Grabs go to the enabled client with the
    best priority whose media kinds match; test it with `POST .../{id}/test`."""
    svc = _svc(request)
    row = await svc.create_client(body.model_dump(), actor=p.actor())
    response.headers["Location"] = f"{request.url.path}/{row.id}"
    return _client_out(svc, row, response)


@router.delete(
    "/download-clients/{client_id}",
    status_code=204,
    dependencies=_write,
    responses=pr(404, 409, 412),
)
async def delete_client(
    client_id: str, request: Request, p: CurrentPrincipal, if_match: IfMatch = None
) -> Response:
    """409 for the built-in engine, or while the client still has unfinished downloads."""
    await _svc(request).delete_client(client_id, if_match=if_match, actor=p.actor())
    return Response(status_code=204)


@router.get("/download-clients", response_model=list[ClientOut], dependencies=_read)
async def list_clients(request: Request) -> list[ClientOut]:
    svc = _svc(request)
    return [_client_out(svc, r) for r in await svc.client_rows()]


@router.get(
    "/download-clients/{client_id}", response_model=ClientOut, dependencies=_read, responses=pr(404)
)
async def get_client(client_id: str, request: Request, response: Response) -> ClientOut:
    svc = _svc(request)
    return _client_out(svc, await svc.client_row(client_id), response)


@router.patch(
    "/download-clients/{client_id}",
    response_model=ClientOut,
    dependencies=_write,
    responses=pr(404, 412, 422),
)
async def patch_client(
    client_id: str,
    body: ClientPatch,
    request: Request,
    response: Response,
    p: CurrentPrincipal,
    if_match: IfMatch = None,
) -> ClientOut:
    """Settings changes restart the client (the built-in engine keeps its torrents)."""
    svc = _svc(request)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    row = await svc.update_client(client_id, changes, if_match=if_match, actor=p.actor())
    return _client_out(svc, row, response)


@router.post(
    "/download-clients/{client_id}/test",
    response_model=TestResult,
    dependencies=_write,
    responses=pr(404),
)
async def test_client(client_id: str, request: Request) -> TestResult:
    return await _svc(request).test_client(client_id)
