"""Music API: find and add artists, their albums with tracks, and album editions.

Artists are titles (kind ``music``): listing, monitoring, deleting and history use the
same /titles and /wanted endpoints as movies and series. Each album is one wanted item.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from fetchbox.core.db import utcnow
from fetchbox.core.errors import problem_responses as pr
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.media.music import MusicService
from fetchbox.modules.media.router import TitleOut, WantedOut, _folder, _one

router = APIRouter(tags=["music"])
_read = [Depends(require("media:read"))]
_write = [Depends(require("media:write"))]

AlbumKind = Literal["album", "ep", "single", "live", "compilation", "other"]


def _music(request: Request) -> MusicService:
    svc: MusicService = request.app.state.music
    return svc


class ArtistHit(BaseModel):
    mbid: str
    name: str
    sort_name: str
    type: str | None = Field(description="Person, Group, Orchestra, ...")
    country: str | None
    disambiguation: str = Field(description='What tells same-named artists apart: "UK band"')
    begin_year: int | None
    in_library: str | None = Field(description="The title id, when already added")


@router.get("/music/search", response_model=list[ArtistHit], dependencies=_read, responses=pr(409))
async def search_artists(
    request: Request, q: str = Query(min_length=1, max_length=200)
) -> list[ArtistHit]:
    """Find an artist on MusicBrainz."""
    music = _music(request)
    have = {t.mbid: t.id for t in await music.artists()}
    return [
        ArtistHit(
            mbid=a.id,
            name=a.name,
            sort_name=a.sort_name,
            type=a.type,
            country=a.country,
            disambiguation=a.disambiguation,
            begin_year=a.begin_year,
            in_library=have.get(a.id),
        )
        for a in await music.search(q)
    ]


class ArtistIn(BaseModel):
    mbid: str = Field(min_length=36, max_length=36, description="MusicBrainz artist id")
    profile_id: str
    monitor_mode: Literal["all", "future", "latest", "none"] = Field(
        default="all",
        description="all; future: released after adding; latest: the newest and future ones",
    )
    album_kinds: list[AlbumKind] = Field(
        default=["album"], description="Which kinds are wanted (studio albums by default)"
    )
    search: bool = Field(default=False, description="Search for wanted albums now")
    folder: str | None = Field(
        default=None,
        max_length=4000,
        description="Its own library folder. Empty, or the default folder for its kind: "
        "the one set in Settings (and it follows that). An existing folder is reused; "
        "another path becomes a new library folder",
    )


@router.post(
    "/music/artists",
    status_code=201,
    response_model=TitleOut,
    dependencies=_write,
    responses=pr(409, 422),
)
async def add_artist(
    body: ArtistIn, request: Request, response: Response, p: CurrentPrincipal
) -> TitleOut:
    """Add an artist with all their albums; the chosen kinds are wanted per the mode."""
    music = _music(request)
    hours = request.app.state.config.current.search.missing_every_hours
    later = None if body.search else utcnow() + timedelta(hours=hours)
    t = await music.add_artist(
        body.mbid,
        profile_id=body.profile_id,
        monitor_mode=body.monitor_mode,
        album_kinds=list(body.album_kinds),
        actor=p.actor(),
        search_after=later,
        folder=_folder(request, "music", body.folder, p),
    )
    response.status_code = 201
    return await _one(request.app.state.media, t, response)


class TrackOut(BaseModel):
    id: str
    disc: int
    number: int
    name: str
    length_ms: int | None


class AlbumOut(BaseModel):
    id: str
    mbid: str
    name: str
    kind: AlbumKind
    secondary_types: list[str]
    release_date: date | None
    cover_url: str | None
    edition_mbid: str | None = Field(description="The edition whose track list is wanted")
    editions: int = Field(description="How many editions MusicBrainz knows (0: not looked up)")
    wanted: WantedOut
    tracks: list[TrackOut]


@router.get(
    "/titles/{title_id}/albums",
    response_model=list[AlbumOut],
    dependencies=_read,
    responses=pr(404),
)
async def list_albums(title_id: str, request: Request) -> list[AlbumOut]:
    """An artist's albums, newest first, each with its wanted item and tracks."""
    return [
        AlbumOut(
            id=a.id,
            mbid=a.mbid,
            name=a.name,
            kind=a.kind,
            secondary_types=a.secondary_types or [],
            release_date=a.release_date,
            cover_url=(a.meta or {}).get("cover"),
            edition_mbid=a.edition_mbid,
            editions=len((a.meta or {}).get("editions") or []),
            wanted=WantedOut.model_validate(w),
            tracks=[
                TrackOut(id=t.id, disc=t.disc, number=t.number, name=t.name, length_ms=t.length_ms)
                for t in tracks
            ],
        )
        for a, w, tracks in await _music(request).albums(title_id)
    ]


class EditionOut(BaseModel):
    mbid: str
    title: str
    date: date | None
    country: str | None
    formats: list[str]
    tracks: int
    length_ms: int


@router.get(
    "/albums/{album_id}/editions",
    response_model=list[EditionOut],
    dependencies=_read,
    responses=pr(404, 409),
)
async def list_editions(album_id: str, request: Request) -> list[EditionOut]:
    """The album's official editions (asks MusicBrainz)."""
    return [
        EditionOut(
            mbid=e.id,
            title=e.title,
            date=e.date,
            country=e.country,
            formats=e.formats,
            tracks=e.track_count,
            length_ms=e.length_ms,
        )
        for e in await _music(request).editions(album_id)
    ]


class EditionIn(BaseModel):
    edition_mbid: str = Field(min_length=36, max_length=36)


@router.put(
    "/albums/{album_id}/edition", status_code=204, dependencies=_write, responses=pr(404, 422)
)
async def choose_edition(album_id: str, body: EditionIn, request: Request) -> Response:
    """Want this edition's track list (e.g. the deluxe one)."""
    await _music(request).choose_edition(album_id, body.edition_mbid)
    return Response(status_code=204)
