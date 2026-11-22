"""Music: artists (titles of kind ``music``), their albums and the tracks of each album.

An artist is added from MusicBrainz with all of its albums (release groups). Each album
is one wanted item, the same unit as a movie or an episode, so music goes through the
same states, queue, import, history and "download again" as video.

Which albums are wanted is the artist's monitor mode (``all``, ``future``: released
after adding, ``latest``: the newest one and future ones, ``none``) applied to the album
types chosen for the artist (studio albums only, by default: no singles, live records
or compilations). An album's track list comes from one edition (a MusicBrainz release),
picked automatically and changeable; it's fetched in the background, at MusicBrainz's
pace, only for albums that are wanted.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, NotFoundError
from fetchbox.core.events import Actor, EventBus
from fetchbox.core.jobs import JobContext, JobRunner
from fetchbox.core.schedules import ScheduleDef, Scheduler
from fetchbox.modules.media.models import ALBUM_KINDS, Album, Title, Track, WantedItem
from fetchbox.modules.media.service import MediaService, _invalid
from fetchbox.providers.metadata.musicbrainz import (
    AlbumResult,
    ArtistResult,
    EditionResult,
    MusicBrainzClient,
    MusicBrainzError,
    cover_url,
    pick_edition,
)

log = structlog.get_logger(__name__)

ArtistMode = Literal["all", "future", "latest", "none"]
ARTIST_MODES: tuple[str, ...] = ("all", "future", "latest", "none")
DEFAULT_KINDS = ["album"]
EDITIONS_JOB = "music.editions"
REFRESH_JOB = "music.refresh"


def monitor_flags(
    mode: str, kinds: list[str], albums: list[tuple[str, str, date | None]], today: date
) -> dict[str, bool]:
    """(id, kind, release date) per album -> monitored, for an artist's mode and kinds."""
    chosen = [a for a in albums if a[1] in kinds]
    released = [a for a in chosen if a[2] is not None and a[2] <= today]
    latest = max(released, key=lambda a: a[2] or date.min, default=None)
    out = {}
    for aid, kind, released_on in albums:
        future = released_on is None or released_on > today
        out[aid] = kind in kinds and (
            mode == "all"
            or (mode == "future" and future)
            or (mode == "latest" and (future or (latest is not None and aid == latest[0])))
        )
    return out


def wanted_flags(
    mode: str, kinds: list[str], albums: list[AlbumResult], today: date
) -> dict[str, bool]:
    """Album id -> monitored, for a newly added artist."""
    return monitor_flags(mode, kinds, [(a.id, a.kind, a.release_date) for a in albums], today)


def _poster(albums: list[AlbumResult]) -> str | None:
    """An artist's picture: the cover of their newest studio album (MusicBrainz has no
    artist images)."""
    studio = [a for a in albums if a.kind == "album" and a.release_date]
    newest = max(studio, key=lambda a: a.release_date or date.min, default=None)
    return cover_url(newest.id, 500) if newest else None


class MusicService:
    def __init__(self, db: Database, bus: EventBus, jobs: JobRunner, media: MediaService) -> None:
        self.db = db
        self.bus = bus
        self.jobs = jobs
        self.media = media
        self.transport: Any = None  # tests: a fake MusicBrainz
        self.interval: float | None = None  # tests: no 1 s spacing
        self._locks: dict[str, asyncio.Lock] = {}

    def client(self) -> MusicBrainzClient:
        kw: dict[str, Any] = {"transport": self.transport}
        if self.interval is not None:
            kw["interval"] = self.interval
        return MusicBrainzClient(**kw)

    def register_jobs(self, scheduler: Scheduler) -> None:
        @self.jobs.handler(EDITIONS_JOB, queue="net", max_attempts=3)
        async def editions(ctx: JobContext) -> dict[str, Any]:
            return await self.fill_editions(progress=ctx.progress)

        @self.jobs.handler(REFRESH_JOB, queue="net", max_attempts=1)
        async def refresh(ctx: JobContext) -> dict[str, Any]:
            added = 0
            for t in await self.artists():
                try:
                    added += await self.refresh_artist(t.id)
                except MusicBrainzError as exc:
                    log.warning("music.refresh_failed", artist=t.name, error=exc.message)
            await self.fill_editions()
            return {"albums_added": added}

        scheduler.define(
            ScheduleDef(
                "music_refresh",
                REFRESH_JOB,
                timedelta(days=1),
                "Look for new albums of the artists in the library (MusicBrainz)",
            )
        )

    # ---- lookups ------------------------------------------------------------------------------

    async def search(self, query: str) -> list[ArtistResult]:
        mb = self.client()
        try:
            return await mb.search_artists(query)
        finally:
            await mb.aclose()

    async def artists(self) -> list[Title]:
        async with self.db.sessionmaker() as s:
            return list((await s.execute(select(Title).where(Title.kind == "music"))).scalars())

    async def albums(self, title_id: str) -> list[tuple[Album, WantedItem, list[Track]]]:
        async with self.db.sessionmaker() as s:
            rows = (
                await s.execute(
                    select(Album, WantedItem)
                    .join(WantedItem, WantedItem.album_id == Album.id)
                    .where(Album.title_id == title_id)
                    .order_by(Album.release_date.desc().nulls_first(), Album.name)
                )
            ).all()
            tracks: dict[str, list[Track]] = {}
            ids = [a.id for a, _ in rows]
            if ids:
                for t in (
                    await s.execute(
                        select(Track)
                        .where(Track.album_id.in_(ids))
                        .order_by(Track.disc, Track.number)
                    )
                ).scalars():
                    tracks.setdefault(t.album_id, []).append(t)
        return [(a, w, tracks.get(a.id, [])) for a, w in rows]

    # ---- add / refresh -----------------------------------------------------------------------

    async def add_artist(
        self,
        mbid: str,
        *,
        profile_id: str,
        monitor_mode: str = "all",
        album_kinds: list[str] | None = None,
        actor: Actor,
        search_after: datetime | None = None,
    ) -> Title:
        if monitor_mode not in ARTIST_MODES:
            raise _invalid("body.monitor_mode", f"one of {', '.join(ARTIST_MODES)}")
        kinds = list(album_kinds or DEFAULT_KINDS)
        if bad := [k for k in kinds if k not in ALBUM_KINDS]:
            raise _invalid("body.album_kinds", f"unknown: {', '.join(bad)}")
        await self.media._check_profile(profile_id, "music")
        async with self.db.sessionmaker() as s:
            dup = (
                await s.execute(select(Title).where(Title.kind == "music", Title.mbid == mbid))
            ).scalar_one_or_none()
            if dup is not None:
                raise ConflictError(f"{dup.name} is already in the library: {dup.id}")
        mb = self.client()
        try:
            artist = await mb.artist(mbid)
            albums = await mb.albums(mbid)
        except MusicBrainzError as exc:
            raise ConflictError(exc.message) from None
        finally:
            await mb.aclose()
        flags = wanted_flags(monitor_mode, kinds, albums, utcnow().date())
        title = Title(
            kind="music",
            mbid=artist.id,
            name=artist.name,
            sort_name=artist.sort_name,
            year=artist.begin_year,
            profile_id=profile_id,
            monitor_mode=monitor_mode,
            refreshed_at=utcnow(),
            meta={
                "poster_url": _poster(albums),
                "album_kinds": kinds,
                "type": artist.type,
                "country": artist.country,
                "disambiguation": artist.disambiguation,
            },
        )
        async with self.db.sessionmaker() as s:
            s.add(title)
            await s.flush()
            for a in albums:
                await self._add_album(s, title, a, flags[a.id], search_after)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                raise ConflictError(f"{artist.name} is already in the library") from None
        await self.media._emit("title.added", title, actor, albums=len(albums))
        await self.jobs.enqueue(EDITIONS_JOB, {}, dedupe_key=EDITIONS_JOB)
        return title

    async def _add_album(
        self, s: Any, title: Title, a: AlbumResult, monitored: bool, search_after: datetime | None
    ) -> None:
        album = Album(
            title_id=title.id,
            mbid=a.id,
            name=a.title,
            kind=a.kind,
            secondary_types=a.secondary_types,
            release_date=a.release_date,
            meta={"cover": cover_url(a.id), "primary_type": a.primary_type},
        )
        s.add(album)
        await s.flush()
        s.add(
            WantedItem(
                title_id=title.id,
                album_id=album.id,
                monitored=monitored,
                next_search_at=search_after,
            )
        )

    async def refresh_artist(self, title_id: str) -> int:
        """New albums of the artist; wanted when of a chosen type (mode not ``none``)."""
        async with self.db.sessionmaker() as s:
            title = await s.get(Title, title_id)
            if title is None or title.kind != "music" or not title.mbid:
                raise NotFoundError("artist", title_id)
            known = set(
                (await s.execute(select(Album.mbid).where(Album.title_id == title_id))).scalars()
            )
        mb = self.client()
        try:
            albums = await mb.albums(title.mbid)
        finally:
            await mb.aclose()
        kinds = (title.meta or {}).get("album_kinds") or DEFAULT_KINDS
        new = [a for a in albums if a.id not in known]
        async with self.db.sessionmaker() as s:
            t = await s.get(Title, title_id)
            assert t is not None
            for a in new:
                await self._add_album(
                    s, t, a, title.monitor_mode != "none" and a.kind in kinds, None
                )
            t.refreshed_at = utcnow()
            await s.commit()
        return len(new)

    # ---- editions ----------------------------------------------------------------------------

    async def fill_editions(self, *, progress: Any = None) -> dict[str, Any]:
        """Pick an edition (and so a track list) for wanted albums that have none yet."""
        async with self.db.sessionmaker() as s:
            todo = list(
                (
                    await s.execute(
                        select(Album.id)
                        .join(WantedItem, WantedItem.album_id == Album.id)
                        .where(WantedItem.monitored.is_(True), Album.edition_mbid.is_(None))
                    )
                ).scalars()
            )
        done = 0
        for n, album_id in enumerate(todo, 1):
            try:
                await self.choose_edition(album_id, only_if_unset=True)
                done += 1
            except MusicBrainzError as exc:
                log.warning("music.editions_failed", album_id=album_id, error=exc.message)
            if progress:
                await progress(n / len(todo), f"{n} of {len(todo)}")
        return {"albums": done}

    async def editions(self, album_id: str) -> list[EditionResult]:
        album = await self._album(album_id)
        mb = self.client()
        try:
            return await mb.editions(album.mbid)
        finally:
            await mb.aclose()

    async def choose_edition(
        self, album_id: str, edition_mbid: str | None = None, *, only_if_unset: bool = False
    ) -> Album:
        """Use ``edition_mbid`` (or the standard edition) as the album's track list.
        ``only_if_unset``: the background pick, which never replaces a chosen edition."""
        lock = self._locks.setdefault(album_id, asyncio.Lock())
        async with lock:  # one change at a time per album: track lists never interleave
            return await self._choose(album_id, edition_mbid, only_if_unset)

    async def _choose(self, album_id: str, edition_mbid: str | None, only_if_unset: bool) -> Album:
        if only_if_unset and (await self._album(album_id)).edition_mbid:
            return await self._album(album_id)
        eds = await self.editions(album_id)
        if edition_mbid is None:
            chosen = pick_edition(eds)
        else:
            chosen = next((e for e in eds if e.id == edition_mbid), None)
            if chosen is None:
                raise _invalid("body.edition", "not an edition of this album")
        async with self.db.sessionmaker() as s:
            album = await s.get(Album, album_id)
            assert album is not None
            if only_if_unset and album.edition_mbid:
                return album  # chosen meanwhile (by the user): keep it
            album.meta = {
                **(album.meta or {}),
                # every edition's shape, to tell which one a download is
                "editions": [
                    {
                        "id": e.id,
                        "tracks": e.track_count,
                        "length_ms": e.length_ms,
                        "country": e.country,
                        "date": e.date.isoformat() if e.date else None,
                        "formats": e.formats,
                    }
                    for e in eds
                ],
            }
            if chosen is not None:
                album.edition_mbid = chosen.id
                await s.execute(delete(Track).where(Track.album_id == album_id))
                for t in chosen.tracks:
                    s.add(
                        Track(
                            album_id=album_id,
                            disc=t.disc,
                            number=t.number,
                            name=t.title,
                            length_ms=t.length_ms,
                            recording_mbid=t.recording_id or None,
                        )
                    )
            await s.commit()
            await s.refresh(album)
            return album

    async def _album(self, album_id: str) -> Album:
        async with self.db.sessionmaker() as s:
            album = await s.get(Album, album_id)
        if album is None:
            raise NotFoundError("album", album_id)
        return album
