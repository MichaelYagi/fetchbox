from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field, computed_field, field_validator

from fetchbox.core.concurrency import etag
from fetchbox.core.errors import ProblemError
from fetchbox.core.errors import problem_responses as pr
from fetchbox.core.paths import host_path
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.downloads.models import Download
from fetchbox.modules.library.existing import ExistingLibrary
from fetchbox.modules.library.maintenance import LibraryCheck
from fetchbox.modules.library.migrate import Migrator
from fetchbox.modules.library.models import MediaFile, NamingTemplate
from fetchbox.modules.library.naming import (
    KIND_KEYS,
    PATTERN_KEYS,
    PRESETS,
    SEASON_FOLDERS,
    TOKENS,
    movie_path,
    preset_of,
    preview_samples,
    render,
    safe_component,
)
from fetchbox.modules.library.relocate import FolderMover
from fetchbox.modules.library.service import LibraryService
from fetchbox.modules.media.series import SeriesMode
from fetchbox.modules.system.jobs_router import JobOut, accepted

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


# ---- library settings: one naming per kind, presets, preview, rename existing ---------------

LibKind = Literal["movie", "series"]


class PresetOut(BaseModel):
    id: str
    label: str
    description: str
    patterns: dict[str, str]


class SeasonFolderOut(BaseModel):
    pattern: str
    label: str


class SampleOut(BaseModel):
    label: str
    path: str


class FolderOut(BaseModel):
    destination_id: str
    path: str
    status: str
    status_message: str | None


class NamingOut(BaseModel):
    kind: LibKind
    folder: FolderOut
    patterns: dict[str, str] = Field(description="Only this kind's patterns")
    preset: str = Field(description="A preset id, or custom")
    samples: list[SampleOut]


class NamingOptionsOut(BaseModel):
    presets: list[PresetOut]
    season_folders: list[SeasonFolderOut]
    tokens: dict[str, str]


class NamingIn(BaseModel):
    patterns: dict[str, str]


class RenameStepOut(BaseModel):
    file_id: str
    title_id: str
    title: str
    current: str
    new: str


class RenameIn(BaseModel):
    file_ids: list[str] | None = Field(default=None, description="Null: every file listed")


class RenameFailureOut(BaseModel):
    file_id: str
    current: str
    error: str


class RenameOut(BaseModel):
    renamed: list[RenameStepOut]
    failed: list[RenameFailureOut]


def _keys(kind: str) -> tuple[str, ...]:
    return (*KIND_KEYS[kind], *(("season_folder",) if kind == "series" else ()))


def _naming(kind: str, dest: Any, patterns: dict[str, str]) -> NamingOut:
    return NamingOut(
        kind=kind,
        folder=FolderOut(
            destination_id=dest.id,
            path=dest.path,
            status=dest.status,
            status_message=dest.status_message,
        ),
        patterns={k: patterns[k] for k in _keys(kind)},
        preset=preset_of(kind, patterns),
        samples=[SampleOut(**x) for x in preview_samples(kind, patterns)],
    )


@router.get("/library/naming-options", response_model=NamingOptionsOut, dependencies=_read)
async def naming_options() -> NamingOptionsOut:
    """Naming presets, season folder choices and the tokens patterns can use."""
    return NamingOptionsOut(
        presets=[
            PresetOut(
                id=pid,
                label=p["label"],
                description=p["description"],
                patterns={k: v for k, v in p.items() if k in PATTERN_KEYS},
            )
            for pid, p in PRESETS.items()
        ],
        season_folders=[SeasonFolderOut(pattern=p, label=lb) for p, lb in SEASON_FOLDERS],
        tokens=TOKENS,
    )


@router.get(
    "/library/naming/{kind}", response_model=NamingOut, dependencies=_read, responses=pr(409)
)
async def get_naming(kind: LibKind, request: Request) -> NamingOut:
    """How new movies or episodes are named and where they go, with example names."""
    dest, patterns = await _svc(request).naming(kind)
    return _naming(kind, dest, patterns)


@router.put(
    "/library/naming/{kind}",
    response_model=NamingOut,
    dependencies=_profiles_write,
    responses=pr(409, 422),
)
async def put_naming(
    kind: LibKind, body: NamingIn, request: Request, p: CurrentPrincipal
) -> NamingOut:
    """Save the naming for movies or series. Patterns of the other kind are ignored.
    Files already in the library keep their names until renamed."""
    dest, patterns = await _svc(request).set_naming(kind, body.patterns, actor=p.actor())
    return _naming(kind, dest, patterns)


@router.post(
    "/library/naming/{kind}/preview",
    response_model=NamingOut,
    dependencies=_read,
    responses=pr(409, 422),
)
async def preview_naming(kind: LibKind, body: NamingIn, request: Request) -> NamingOut:
    """Example names for patterns, before saving them."""
    svc = _svc(request)
    dest, current = await svc.naming(kind)
    changes = {k: v for k, v in body.patterns.items() if k in _keys(kind)}
    return _naming(kind, dest, svc.check_patterns({**current, **changes}))


@router.get("/library/rename/{kind}", response_model=list[RenameStepOut], dependencies=_read)
async def rename_preview(kind: LibKind, request: Request) -> list[RenameStepOut]:
    """Files whose name differs from what the current naming gives, old and new."""
    return [RenameStepOut(**x) for x in await _svc(request).rename_plan(kind)]


@router.post("/library/rename/{kind}", response_model=RenameOut, dependencies=_write)
async def rename_files(
    kind: LibKind, body: RenameIn, request: Request, p: CurrentPrincipal
) -> RenameOut:
    """Rename files to the current naming (on the same disk: nothing is copied)."""
    out = await _svc(request).rename(kind, body.file_ids, actor=p.actor())
    return RenameOut(
        renamed=[RenameStepOut(**x) for x in out["renamed"]],
        failed=[RenameFailureOut(**x) for x in out["failed"]],
    )


# ---- existing library: scan a folder, review, apply ---------------------------------------


class ScanIn(BaseModel):
    destination_id: str


class ImportItemIn(BaseModel):
    folder: str = Field(description="As the scan listed it")
    files: list[str] = Field(min_length=1, description="Paths relative to the library folder")
    tmdb_id: int | None = Field(default=None, description="Movies and series")
    artist_mbid: str | None = Field(default=None, description="Music: the MusicBrainz artist")
    album_mbid: str | None = Field(default=None, description="Music: the release group")
    profile_id: str
    monitored: bool = Field(default=True, description="Movies")
    monitor_mode: SeriesMode = Field(default="all", description="Series")


class ImportIn(BaseModel):
    destination_id: str
    items: list[ImportItemIn] = Field(min_length=1)


def _existing(request: Request) -> ExistingLibrary:
    svc: ExistingLibrary = request.app.state.existing
    return svc


@router.post(
    "/library/scan",
    status_code=202,
    response_model=JobOut,
    dependencies=_write,
    responses=pr(404, 409),
)
async def scan_library(body: ScanIn, request: Request) -> Response:
    """Read a library folder and match what's in it on TMDB. Changes nothing: the job's
    result lists every movie or show found, with its files, match and status."""
    return accepted(await _existing(request).start_scan(body.destination_id))


@router.post(
    "/library/import",
    status_code=202,
    response_model=JobOut,
    dependencies=_write,
    responses=pr(404, 409, 422),
)
async def import_existing(body: ImportIn, request: Request, p: CurrentPrincipal) -> Response:
    """Add the chosen titles (no search yet) and record their files where they are."""
    dest = await request.app.state.storage.destination(body.destination_id)
    for item in body.items:
        await request.app.state.profiles.get_profile(item.profile_id)  # 404 if unknown
        if dest.media_kind == "music" and not (item.artist_mbid and item.album_mbid):
            raise ProblemError(422, "Validation Failed", "music needs artist_mbid and album_mbid",
                               slug="validation")  # fmt: skip
        if dest.media_kind != "music" and not item.tmdb_id:
            raise ProblemError(422, "Validation Failed", "tmdb_id is needed", slug="validation")
    job = await _existing(request).start_apply(
        body.destination_id, [i.model_dump() for i in body.items], actor=p.actor()
    )
    return accepted(job)


# ---- library check: missing files, duplicates, unknown files ------------------------------


class ForgetIn(BaseModel):
    file_ids: list[str] = Field(min_length=1)


class ForgetOut(BaseModel):
    forgotten: int = Field(description="Records dropped; their items are wanted again")


@router.post(
    "/library/check", status_code=202, response_model=JobOut, dependencies=_read,
    responses=pr(409),
)  # fmt: skip
async def check_library(request: Request) -> Response:
    """Look for missing files, duplicates (same size and quick hash) and video files no
    record points to. Changes nothing; the job's result is the report."""
    checker: LibraryCheck = request.app.state.library_check
    return accepted(await checker.start())


@router.post("/library/check/forget", response_model=ForgetOut, dependencies=_write)
async def forget_missing(body: ForgetIn, request: Request, p: CurrentPrincipal) -> ForgetOut:
    """Drop records of files that are missing from disk; their movies and episodes are
    searched for again. Files still on disk are left alone."""
    checker: LibraryCheck = request.app.state.library_check
    return ForgetOut(forgotten=await checker.forget(body.file_ids, actor=p.actor()))


# ---- move from Radarr / Sonarr ------------------------------------------------------------


class MigrateConnectIn(BaseModel):
    kind: Literal["radarr", "sonarr"]
    url: str = Field(min_length=1, description="e.g. http://192.168.1.10:7878")
    api_key: str = Field(min_length=1, description="Settings > General > API Key")


class PathMapIn(BaseModel):
    from_: str = Field(alias="from", min_length=1, description="The path as Radarr/Sonarr sees it")
    to: str = Field(min_length=1, description="The same folder as Fetchbox sees it")


class MigrateApplyIn(MigrateConnectIn):
    path_map: list[PathMapIn] = []
    profile_map: dict[str, str] = Field(
        default={}, description="Their quality profile id -> a Fetchbox profile id"
    )
    only: list[int] | None = Field(default=None, description="Their title ids; null: all")


def _migrator(request: Request) -> Migrator:
    svc: Migrator = request.app.state.migrator
    return svc


@router.post("/library/migrate/scan", status_code=202, response_model=JobOut, dependencies=_write,
             responses=pr(409, 422))  # fmt: skip
async def migrate_scan(body: MigrateConnectIn, request: Request) -> Response:
    """Read what Radarr or Sonarr has (changes nothing on either side). The job's result
    lists titles, files, root folders, profile mapping, indexers and clients."""
    return accepted(await _migrator(request).start_scan(body.kind, body.url, body.api_key))


@router.post("/library/migrate/apply", status_code=202, response_model=JobOut, dependencies=_write,
             responses=pr(409, 422))  # fmt: skip
async def migrate_apply(body: MigrateApplyIn, request: Request, p: CurrentPrincipal) -> Response:
    """Add their titles and record their files in place. Radarr / Sonarr aren't changed."""
    options = {
        "path_map": [{"from": m.from_, "to": m.to} for m in body.path_map],
        "profile_map": body.profile_map,
        "only": body.only,
    }
    job = await _migrator(request).start_apply(
        body.kind, body.url, body.api_key, options, p.actor()
    )
    return accepted(job)


class MoveFolderIn(BaseModel):
    path: str = Field(min_length=1, max_length=4000, description="The new folder")

    @field_validator("path", mode="before")
    @classmethod
    def _host(cls, v: object) -> object:
        r"""Under WSL, a Windows path (C:\Media) is taken as its /mnt/c/Media form."""
        return host_path(v) if isinstance(v, str) else v


@router.post("/destinations/{dest_id}/move", status_code=202, response_model=JobOut,
             dependencies=_write, responses=pr(404, 409, 422))  # fmt: skip
async def move_folder(dest_id: str, body: MoveFolderIn, request: Request,
                      p: CurrentPrincipal) -> Response:  # fmt: skip
    """Move a library folder with its files (copy, check, switch, then delete the old
    copies; safe across drives). Unlike changing the path, the library comes along."""
    mover: FolderMover = request.app.state.folder_mover
    return accepted(await mover.start(dest_id, body.path, p.actor()))
