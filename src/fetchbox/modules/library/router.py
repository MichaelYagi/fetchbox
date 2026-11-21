from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field, computed_field

from fetchbox.core.concurrency import etag
from fetchbox.core.errors import problem_responses as pr
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.downloads.models import Download
from fetchbox.modules.library.models import MediaFile, NamingTemplate
from fetchbox.modules.library.naming import TOKENS, movie_path, render, safe_component
from fetchbox.modules.library.service import LibraryService

router = APIRouter(tags=["library"])
_read = [Depends(require("library:read"))]
_write = [Depends(require("library:write"))]
_profiles_read = [Depends(require("profiles:read"))]
_profiles_write = [Depends(require("profiles:write"))]
IfMatch = Annotated[str | None, Header(description="ETag from a previous read; 412 if stale")]


def _svc(request: Request) -> LibraryService:
    svc: LibraryService = request.app.state.library
    return svc


# ---- files --------------------------------------------------------------------------------


class FileOut(BaseModel):
    id: str
    destination_id: str | None
    relative_path: str
    size: int
    quality: dict[str, Any]
    custom_format_score: int
    release_group: str | None
    original_name: str | None
    link_mode: str | None
    mediainfo: dict[str, Any] | None = Field(
        description="Read from the file: width, height, resolution, codecs, HDR, duration, "
        "languages; null for containers Fetchbox can't read"
    )
    wanted_item_ids: list[str]
    imported_at: datetime


def _file(f: MediaFile, items: list[str]) -> FileOut:
    return FileOut(
        id=f.id,
        destination_id=f.destination_id,
        relative_path=f.relative_path,
        size=f.size,
        quality=f.quality,
        custom_format_score=f.custom_format_score,
        mediainfo=f.mediainfo,
        release_group=f.release_group,
        original_name=f.original_name,
        link_mode=f.link_mode,
        wanted_item_ids=items,
        imported_at=f.imported_at,
    )


@router.get("/files", response_model=list[FileOut], dependencies=_read)
async def list_files(request: Request, title_id: str | None = None) -> list[FileOut]:
    return [_file(f, i) for f, i in await _svc(request).files(title_id)]


@router.get("/titles/{title_id}/files", response_model=list[FileOut], dependencies=_read)
async def title_files(title_id: str, request: Request) -> list[FileOut]:
    return [_file(f, i) for f, i in await _svc(request).files(title_id)]


@router.get("/files/{file_id}", response_model=FileOut, dependencies=_read, responses=pr(404))
async def get_file(file_id: str, request: Request) -> FileOut:
    return _file(*await _svc(request).file(file_id))


@router.delete("/files/{file_id}", status_code=204, dependencies=_write, responses=pr(404, 409))
async def delete_file(
    file_id: str,
    request: Request,
    p: CurrentPrincipal,
    delete_from_disk: bool = Query(True, description="Move the file to the recycle folder"),
) -> Response:
    """The item stays locked: use redownload to get it again."""
    await _svc(request).delete_file(file_id, delete_from_disk=delete_from_disk, actor=p.actor())
    return Response(status_code=204)


# ---- imports ------------------------------------------------------------------------------


class ImportOut(BaseModel):
    download_id: str
    name: str
    status: str | None = Field(description="pending, importing, imported, needs_attention, skipped")
    error: str | None
    attempts: int
    retry_at: datetime | None
    imported_at: datetime | None
    completed_at: datetime | None


def _imp(d: Download) -> ImportOut:
    return ImportOut(
        download_id=d.id,
        name=d.name,
        status=d.import_status,
        error=d.import_error,
        attempts=d.import_attempts,
        retry_at=d.import_retry_at,
        imported_at=d.imported_at,
        completed_at=d.completed_at,
    )


@router.get("/imports", response_model=list[ImportOut], dependencies=_read)
async def list_imports(request: Request) -> list[ImportOut]:
    """Imports of completed downloads, newest first; `needs_attention` ones say why."""
    return [_imp(d) for d in await _svc(request).imports()]


@router.post(
    "/imports/{download_id}/retry", status_code=202, dependencies=_write, responses=pr(404, 409)
)
async def retry_import(download_id: str, request: Request) -> Response:
    await _svc(request).retry(download_id)
    return Response(status_code=202)


# ---- naming templates ---------------------------------------------------------------------


class TemplateOut(BaseModel):
    id: str
    name: str
    patterns: dict[str, str]
    version: int = Field(exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def etag(self) -> str:
        return etag(self.version)


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    patterns: dict[str, str] = Field(default={}, description="Missing patterns use the defaults")


class TemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    patterns: dict[str, str] | None = None


class PreviewIn(BaseModel):
    patterns: dict[str, str] = Field(default={}, description="Overrides on top of the defaults")
    sample: dict[str, Any] | None = Field(default=None, description="Token values; else a sample")


class PreviewOut(BaseModel):
    movie: str
    episode: str
    tokens: dict[str, str]


SAMPLE_MOVIE = {
    "Title": "Dune: Part Two",
    "Year": 2024,
    "TmdbId": 693134,
    "Quality": "WEBDL-2160p",
    "HDR": ["DV", "HDR10"],
    "Codec": "x265",
    "Group": "FLUX",
    "Resolution": "2160p",
    "Source": "WEB-DL",
}
SAMPLE_EPISODE = {
    "Title": "Severance",
    "Year": 2022,
    "TvdbId": 371980,
    "Season": 1,
    "Episode": 7,
    "EpisodeTitle": "Defiant Jazz",
    "Quality": "WEBDL-1080p",
}


def _tpl(t: NamingTemplate, response: Response | None = None) -> TemplateOut:
    out = TemplateOut(id=t.id, name=t.name, patterns=t.patterns, version=t.version)
    if response is not None:
        response.headers["ETag"] = out.etag
    return out


@router.get("/naming-templates", response_model=list[TemplateOut], dependencies=_profiles_read)
async def list_templates(request: Request) -> list[TemplateOut]:
    return [_tpl(t) for t in await _svc(request).templates()]


@router.post(
    "/naming-templates",
    response_model=TemplateOut,
    status_code=201,
    dependencies=_profiles_write,
    responses=pr(409, 422),
)
async def create_template(
    body: TemplateIn, request: Request, response: Response, p: CurrentPrincipal
) -> TemplateOut:
    t = await _svc(request).create_template(body.name, body.patterns, actor=p.actor())
    response.headers["Location"] = f"{request.url.path}/{t.id}"
    return _tpl(t, response)


@router.patch(
    "/naming-templates/{template_id}",
    response_model=TemplateOut,
    dependencies=_profiles_write,
    responses=pr(404, 412, 422),
)
async def update_template(
    template_id: str,
    body: TemplatePatch,
    request: Request,
    response: Response,
    p: CurrentPrincipal,
    if_match: IfMatch = None,
) -> TemplateOut:
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    t = await _svc(request).update_template(
        template_id, changes, if_match=if_match, actor=p.actor()
    )
    return _tpl(t, response)


@router.post(
    "/naming-templates/preview",
    response_model=PreviewOut,
    dependencies=_profiles_read,
    responses=pr(422),
)
async def preview(body: PreviewIn) -> PreviewOut:
    """Render patterns before saving them."""
    patterns = LibraryService.check_patterns(body.patterns)
    movie_ctx = body.sample or SAMPLE_MOVIE
    ep_ctx = body.sample or SAMPLE_EPISODE
    keys = ("series_folder", "season_folder", "episode_file")
    parts = [safe_component(render(patterns[k], ep_ctx)) for k in keys]
    episode = "/".join(parts) + ".mkv"
    return PreviewOut(movie=movie_path(patterns, movie_ctx, "mkv"), episode=episode, tokens=TOKENS)
