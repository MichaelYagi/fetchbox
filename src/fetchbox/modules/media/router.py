from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal, get_args

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, computed_field

from fetchbox.core.concurrency import etag
from fetchbox.core.db import utcnow
from fetchbox.core.errors import FieldError, ProblemError
from fetchbox.core.errors import problem_responses as pr
from fetchbox.core.pagination import Page, Sort, paginate
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.library.naming import safe_component
from fetchbox.modules.media.models import HISTORY_TYPES, HistoryEvent, Title, WantedItem
from fetchbox.modules.media.series import SeriesMode
from fetchbox.modules.media.service import (
    CALENDAR_MAX_DAYS,
    MediaService,
    WantedFilter,
    _invalid,
    available_from,
    calendar,
)

Availability = Literal["announced", "in_cinemas", "released"]
router = APIRouter(tags=["media"])
_read = [Depends(require("media:read"))]
_write = [Depends(require("media:write"))]
IfMatch = Annotated[str | None, Header(description="ETag from a previous read; 412 if stale")]


def _svc(request: Request) -> MediaService:
    svc: MediaService = request.app.state.media
    return svc


class _Versioned(BaseModel):
    version: int = Field(exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def etag(self) -> str:
        return etag(self.version)


class WantedOut(_Versioned):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title_id: str
    episode_id: str | None
    album_id: str | None = None
    state: str
    monitored: bool
    lock: str | None
    cutoff_met: bool
    last_search_at: datetime | None
    next_search_at: datetime | None


class SeriesStats(BaseModel):
    episodes: int = Field(description="Excluding unmonitored specials")
    monitored: int
    acquired: int
    missing: int = Field(description="Monitored, aired, no file yet")


class AlbumStats(BaseModel):
    albums: int
    monitored: int
    acquired: int
    missing: int = Field(description="Monitored, released, no files yet")


class TitleOut(_Versioned):
    id: str
    kind: str
    mbid: str | None = Field(default=None, description="Music: the MusicBrainz artist id")
    tmdb_id: int | None
    imdb_id: str | None
    tvdb_id: int | None
    name: str
    sort_name: str
    year: int | None
    status: str | None
    profile_id: str
    destination_id: str | None
    monitor_mode: str
    original_name: str | None = None
    original_language: str | None = None
    runtime: int | None = None
    overview: str = ""
    poster_url: str | None = None
    genres: list[str] = []
    alt_titles: list[str] = []
    added_at: datetime
    refreshed_at: datetime | None
    wanted: list[WantedOut] = Field(default=[], description="For a movie, its one wanted item")
    minimum_availability: str
    available_from: date | None = Field(
        default=None,
        description="Movies: when automatic search starts (null: now)",
    )
    theatrical_date: date | None = None
    digital_date: date | None = None
    physical_date: date | None = None
    episodes: SeriesStats | None = Field(
        default=None, description="For a series: counts (see /titles/{id}/episodes for the list)"
    )
    albums: AlbumStats | None = Field(
        default=None, description="For an artist: counts (see /titles/{id}/albums for the list)"
    )


def title_out(
    t: Title,
    wanted: list[WantedItem] | None = None,
    stats: dict[str, int] | None = None,
    album_stats: dict[str, int] | None = None,
) -> TitleOut:
    meta = t.meta or {}
    return TitleOut(
        id=t.id,
        version=t.version,
        kind=t.kind,
        mbid=t.mbid,
        tmdb_id=t.tmdb_id,
        imdb_id=t.imdb_id,
        tvdb_id=t.tvdb_id,
        name=t.name,
        sort_name=t.sort_name,
        year=t.year,
        status=t.status,
        profile_id=t.profile_id,
        destination_id=t.destination_id,
        monitor_mode=t.monitor_mode,
        minimum_availability=t.minimum_availability,
        available_from=_future(available_from(t)),
        theatrical_date=meta.get("theatrical_date"),
        digital_date=meta.get("digital_date"),
        physical_date=meta.get("physical_date"),
        original_name=meta.get("original_name"),
        original_language=meta.get("original_language"),
        runtime=meta.get("runtime"),
        overview=meta.get("overview") or "",
        poster_url=meta.get("poster_url"),
        genres=meta.get("genres") or [],
        alt_titles=meta.get("alt_titles") or [],
        added_at=t.added_at,
        refreshed_at=t.refreshed_at,
        wanted=[WantedOut.model_validate(w) for w in wanted or []] if t.kind == "movie" else [],
        episodes=SeriesStats(**stats) if stats is not None else None,
        albums=AlbumStats(**album_stats) if album_stats is not None else None,
    )


async def _many(svc: MediaService, titles: list[Title]) -> list[TitleOut]:
    movies = [t.id for t in titles if t.kind == "movie"]
    wanted = await svc.wanted_for(movies)
    stats = await svc.series_stats([t.id for t in titles if t.kind == "series"])
    albums = await svc.album_stats([t.id for t in titles if t.kind == "music"])
    return [title_out(t, wanted.get(t.id, []), stats.get(t.id), albums.get(t.id)) for t in titles]


def _future(d: date | None) -> date | None:
    return d if d is not None and d > utcnow().date() else None


async def _one(svc: MediaService, t: Title, response: Response | None = None) -> TitleOut:
    [out] = await _many(svc, [t])
    if response is not None:
        response.headers["ETag"] = out.etag
    return out


# ---- titles -------------------------------------------------------------------------------


class TitlePage(Page[TitleOut]):
    pass


TITLE_SORT = Sort(
    {"sort_name": Title.sort_name, "added_at": Title.added_at, "year": Title.year, "id": Title.id},
    id_col=Title.id,
    default="sort_name",
)


class TitleIn(BaseModel):
    kind: Literal["movie", "series"] = "movie"
    tmdb_id: int = Field(gt=0)
    profile_id: str
    monitored: bool = Field(default=True, description="Movies: monitored or not")
    monitor_mode: SeriesMode = Field(
        default="all",
        description="Series: all, future, missing, first_season, latest_season or none",
    )
    minimum_availability: Availability = Field(
        default="released",
        description="Movies: start searching once it is announced, in cinemas, or released "
        "(digital or physical)",
    )
    search: bool = Field(
        default=True,
        description="Search for it now; otherwise at the regular missing-search interval",
    )
    folder: str | None = Field(
        default=None,
        max_length=4000,
        description="Its own library folder. Empty, or the default folder for its kind: "
        "the one set in Settings (and it follows that). An existing folder is reused; "
        "another path becomes a new library folder",
    )
    title_folder: str | None = Field(
        default=None,
        max_length=255,
        description="Its own folder inside the library folder (an existing one: new files "
        "join it). Empty: the naming decides, when the first file is filed",
    )


class TitlePatch(BaseModel):
    profile_id: str | None = None
    monitor_mode: str | None = Field(default=None, min_length=1, max_length=20)
    minimum_availability: Availability | None = None


@router.get("/titles", response_model=TitlePage, dependencies=_read, responses=pr(422))
async def list_titles(
    request: Request,
    kind: Literal["movie", "series", "music"] | None = Query(None, alias="filter[kind]"),
    sort: str | None = Query(None, description=f"One of {', '.join(TITLE_SORT.options)}"),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> TitlePage:
    svc = _svc(request)
    async with svc.db.sessionmaker() as s:
        rows, nxt = await paginate(
            s, svc.list_query(kind), TITLE_SORT, sort_param=sort, limit=limit, cursor=cursor
        )
    return TitlePage(items=await _many(svc, list(rows)), next_cursor=nxt)


def own_folder(name: str | None, field: str = "body.title_folder") -> str | None:
    """A title's own folder: one name inside its library folder, or None."""
    name = (name or "").strip()
    if not name:
        return None
    if "/" in name or "\\" in name or name in (".", "..") or safe_component(name) != name:
        raise _invalid(field, "one folder name inside the library folder, like The Bear")
    return name


def _folder(
    request: Request, kind: str, path: str | None, p: Any
) -> Callable[[], Awaitable[str | None]] | None:
    """Resolves the folder asked for, once the add has passed its own checks."""
    if not path or not path.strip():
        return None
    storage = request.app.state.storage

    async def resolve() -> str | None:
        dest: str | None = await storage.folder_for(
            kind, path, field="body.folder", actor=p.actor()
        )
        return dest

    return resolve


@router.post(
    "/titles",
    response_model=TitleOut,
    status_code=201,
    dependencies=_write,
    responses=pr(404, 409, 422, 502, 503),
)
async def add_title(
    body: TitleIn, request: Request, response: Response, p: CurrentPrincipal
) -> TitleOut:
    """Add a movie or series by TMDB id with a quality profile. A movie gets one wanted
    item; a series one per episode, monitored per `monitor_mode` (specials never)."""
    svc = _svc(request)
    hours = request.app.state.config.current.search.missing_every_hours
    later = None if body.search else utcnow() + timedelta(hours=hours)
    folder = _folder(request, body.kind, body.folder, p)
    if body.kind == "series":
        t = await svc.add_series(
            body.tmdb_id,
            profile_id=body.profile_id,
            monitor_mode=body.monitor_mode,
            actor=p.actor(),
            search_after=later,
            folder=folder,
            title_folder=own_folder(body.title_folder),
        )
    else:
        t = await svc.add_movie(
            body.tmdb_id,
            profile_id=body.profile_id,
            monitored=body.monitored,
            actor=p.actor(),
            search_after=later,
            minimum_availability=body.minimum_availability,
            folder=folder,
            title_folder=own_folder(body.title_folder),
        )
    response.headers["Location"] = f"{request.url.path}/{t.id}"
    return await _one(svc, t, response)


@router.get("/titles/{title_id}", response_model=TitleOut, dependencies=_read, responses=pr(404))
async def get_title(title_id: str, request: Request, response: Response) -> TitleOut:
    svc = _svc(request)
    return await _one(svc, await svc.get_title(title_id), response)


@router.patch(
    "/titles/{title_id}",
    response_model=TitleOut,
    dependencies=_write,
    responses=pr(404, 412, 422),
)
async def update_title(
    title_id: str,
    body: TitlePatch,
    request: Request,
    response: Response,
    p: CurrentPrincipal,
    if_match: IfMatch = None,
) -> TitleOut:
    """Change the quality profile or monitor mode (movies: `all` or `none`)."""
    svc = _svc(request)
    changes: dict[str, Any] = body.model_dump(exclude_unset=True, exclude_none=True)
    t = await svc.update_title(title_id, changes, if_match=if_match, actor=p.actor())
    return await _one(svc, t, response)


@router.post(
    "/titles/{title_id}/refresh",
    response_model=TitleOut,
    dependencies=_write,
    responses=pr(404, 409, 502, 503),
)
async def refresh_title(
    title_id: str, request: Request, response: Response, p: CurrentPrincipal
) -> TitleOut:
    """Re-read metadata from TMDB now."""
    svc = _svc(request)
    return await _one(svc, await svc.refresh_title(title_id, actor=p.actor()), response)


class EpisodeOut(BaseModel):
    id: str
    number: int
    absolute_number: int | None
    name: str | None
    air_date: date | None
    wanted: WantedOut


class SeasonOut(BaseModel):
    number: int
    monitored: bool
    episodes: list[EpisodeOut]


class SeasonPatch(BaseModel):
    monitored: bool


@router.get(
    "/titles/{title_id}/episodes",
    response_model=list[SeasonOut],
    dependencies=_read,
    responses=pr(404, 409),
)
async def list_episodes(title_id: str, request: Request) -> list[SeasonOut]:
    """A series' seasons and episodes, each with its wanted item (state, monitored...)."""
    return [
        SeasonOut(
            number=season.number,
            monitored=season.monitored,
            episodes=[
                EpisodeOut(
                    id=ep.id,
                    number=ep.number,
                    absolute_number=ep.absolute_number,
                    name=ep.name,
                    air_date=ep.air_date,
                    wanted=WantedOut.model_validate(w),
                )
                for ep, w in rows
            ],
        )
        for season, rows in await _svc(request).episodes(title_id)
    ]


@router.patch(
    "/titles/{title_id}/seasons/{number}",
    response_model=TitleOut,
    dependencies=_write,
    responses=pr(404),
)
async def update_season(
    title_id: str, number: int, body: SeasonPatch, request: Request, p: CurrentPrincipal
) -> TitleOut:
    """Monitor or unmonitor a whole season (specials included: season 0)."""
    svc = _svc(request)
    await svc.set_season_monitored(title_id, number, body.monitored, actor=p.actor())
    return await _one(svc, await svc.get_title(title_id))


@router.delete("/titles/{title_id}", status_code=204, dependencies=_write, responses=pr(404, 412))
async def delete_title(
    title_id: str, request: Request, p: CurrentPrincipal, if_match: IfMatch = None
) -> Response:
    """Remove the title and its wanted items. Files on disk are not touched."""
    await _svc(request).delete_title(title_id, if_match=if_match, actor=p.actor())
    return Response(status_code=204)


# ---- wanted -------------------------------------------------------------------------------


class WantedRow(WantedOut):
    title_name: str
    title_year: int | None
    title_kind: str
    season_number: int | None = None
    episode_number: int | None = None
    episode_name: str | None = None
    air_date: date | None = None


class WantedPage(Page[WantedRow]):
    pass


WANTED_SORT = Sort(
    {"id": WantedItem.id, "created_at": WantedItem.created_at},
    id_col=WantedItem.id,
    default="-id",
)


_STATE_HELP = "`missing`: not yet acquired; `cutoff_unmet`: acquired below the cutoff"


class WantedPatch(BaseModel):
    monitored: bool


@router.get("/wanted", response_model=WantedPage, dependencies=_read, responses=pr(422))
async def list_wanted(
    request: Request,
    state: Annotated[WantedFilter | None, Query(description=_STATE_HELP)] = None,
    monitored: bool | None = None,
    sort: str | None = Query(None, description=f"One of {', '.join(WANTED_SORT.options)}"),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> WantedPage:
    svc = _svc(request)
    async with svc.db.sessionmaker() as s:
        rows, nxt = await paginate(
            s,
            svc.wanted_query(state, monitored),
            WANTED_SORT,
            sort_param=sort,
            limit=limit,
            cursor=cursor,
        )
    titles = await svc.titles_by_id({w.title_id for w in rows})
    eps = await svc.episode_info({w.episode_id for w in rows if w.episode_id})
    items = []
    for w in rows:
        season, ep = eps.get(w.episode_id or "", (None, None))
        items.append(
            WantedRow(
                **WantedOut.model_validate(w).model_dump(),
                version=w.version,
                title_name=titles[w.title_id].name,
                title_year=titles[w.title_id].year,
                title_kind=titles[w.title_id].kind,
                season_number=season,
                episode_number=ep.number if ep else None,
                episode_name=ep.name if ep else None,
                air_date=ep.air_date if ep else None,
            )
        )
    return WantedPage(items=items, next_cursor=nxt)


@router.patch(
    "/wanted/{wanted_id}",
    response_model=WantedOut,
    dependencies=_write,
    responses=pr(404, 412, 422),
)
async def update_wanted(
    wanted_id: str,
    body: WantedPatch,
    request: Request,
    response: Response,
    p: CurrentPrincipal,
    if_match: IfMatch = None,
) -> WantedOut:
    """Monitor or unmonitor one wanted item."""
    row = await _svc(request).update_wanted(
        wanted_id, monitored=body.monitored, if_match=if_match, actor=p.actor()
    )
    out = WantedOut.model_validate(row)
    response.headers["ETag"] = out.etag
    return out


# ---- history ------------------------------------------------------------------------------


class HistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: str
    title_id: str | None
    wanted_item_id: str | None
    data: dict[str, Any]
    created_at: datetime


class HistoryPage(Page[HistoryOut]):
    pass


HISTORY_SORT = Sort({"id": HistoryEvent.id}, id_col=HistoryEvent.id, default="-id")
HistoryType = Literal["grabbed", "imported", "failed", "deleted", "upgraded", "unlocked"]
assert set(get_args(HistoryType)) == set(HISTORY_TYPES)


async def _history_page(
    svc: MediaService,
    *,
    title_id: str | None,
    wanted_item_id: str | None,
    type_: str | None,
    limit: int,
    cursor: str | None,
) -> HistoryPage:
    q = svc.history_query(title_id=title_id, wanted_item_id=wanted_item_id, type_=type_)
    async with svc.db.sessionmaker() as s:
        rows, nxt = await paginate(s, q, HISTORY_SORT, sort_param=None, limit=limit, cursor=cursor)
    return HistoryPage(items=[HistoryOut.model_validate(r) for r in rows], next_cursor=nxt)


@router.get("/history", response_model=HistoryPage, dependencies=_read, responses=pr(422))
async def list_history(
    request: Request,
    type: HistoryType | None = None,
    title_id: str | None = None,
    wanted_item_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> HistoryPage:
    """Newest first. History outlives the titles it describes."""
    return await _history_page(_svc(request), title_id=title_id, wanted_item_id=wanted_item_id,
                               type_=type, limit=limit, cursor=cursor)  # fmt: skip


@router.get(
    "/titles/{title_id}/history",
    response_model=HistoryPage,
    dependencies=_read,
    responses=pr(404, 422),
)
async def title_history(
    title_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
) -> HistoryPage:
    svc = _svc(request)
    await svc.get_title(title_id)  # 404 for an unknown title
    return await _history_page(svc, title_id=title_id, wanted_item_id=None, type_=None,
                               limit=limit, cursor=cursor)  # fmt: skip


class CalendarEntry(BaseModel):
    date: date
    kind: Literal["episode", "movie", "album", "item"]  # item: a plugin media kind's
    title_id: str
    name: str
    poster_url: str | None
    season: int | None
    episode: int | None
    episode_name: str | None
    release: Literal["cinema", "digital", "disc"] | None = Field(
        description="Movies: which release this date is"
    )
    state: str
    monitored: bool
    album: str | None = Field(default=None, description="Music: the album (name is the artist)")
    media_kind: str | None = Field(
        default=None, description="An item's media kind (a plugin's, such as comics)"
    )


@router.get("/calendar", response_model=list[CalendarEntry], dependencies=_read, responses=pr(422))
async def get_calendar(
    request: Request,
    start: Annotated[date, Query(description="First day (inclusive)")],
    end: Annotated[date, Query(description="Last day (inclusive)")],
) -> list[CalendarEntry]:
    """Episodes airing, movie release dates and wanted albums' release dates between two
    days (at most 100 days)."""
    if end < start or (end - start).days > CALENDAR_MAX_DAYS:
        raise ProblemError(
            422,
            "Validation Failed",
            slug="validation",
            errors=[FieldError(field="query.end", message="end must be 0-100 days after start")],
        )
    return [CalendarEntry(**e) for e in await calendar(_svc(request).db, start, end)]
