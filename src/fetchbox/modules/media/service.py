"""Titles and wanted items. Phase 1 adds movies; series arrive with the TV pipeline."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Select, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fetchbox.core.concurrency import check_if_match
from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, FieldError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.jobs import JobContext, JobRunner
from fetchbox.core.schedules import ScheduleDef, Scheduler
from fetchbox.modules.discovery.service import DiscoveryService
from fetchbox.modules.media.models import Episode, HistoryEvent, Season, Title, WantedItem
from fetchbox.modules.media.series import (
    SERIES_MODES,
    Ep,
    SeriesMode,
    latest_season,
    seasons_monitored,
)
from fetchbox.modules.media.series import for_new as monitor_new
from fetchbox.modules.media.series import initial as monitor_initial
from fetchbox.modules.profiles.service import ProfilesService
from fetchbox.providers.metadata import MovieDetail, SeriesDetail

MOVIE_MONITOR_MODES = ("all", "none")
REFRESH_JOB = "media.refresh_metadata"
STALE_AFTER = timedelta(days=7)
REFRESH_BATCH = 200
SYSTEM = Actor(type="system", id="metadata-refresh", name="metadata refresh")
WantedFilter = Literal["missing", "cutoff_unmet"]
EpisodeRow = tuple[Episode, WantedItem]
_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.I)


def sort_name(name: str) -> str:
    """ "The Matrix" sorts as "matrix"."""
    return _ARTICLE.sub("", name.strip()).casefold()


def _movie_meta(m: MovieDetail) -> dict[str, Any]:
    return {
        "original_name": m.original_name,
        "original_language": m.original_language,
        "runtime": m.runtime,
        "overview": m.overview,
        "poster_url": m.poster_url,
        "genres": m.genres,
        "alt_titles": m.alt_titles,
        "release_date": m.release_date.isoformat() if m.release_date else None,
        "theatrical_date": m.theatrical_date.isoformat() if m.theatrical_date else None,
        "digital_date": m.digital_date.isoformat() if m.digital_date else None,
        "physical_date": m.physical_date.isoformat() if m.physical_date else None,
    }


AVAILABILITY = ("announced", "in_cinemas", "released")
RELEASE_FALLBACK = timedelta(days=90)  # no digital/physical date: cinema date + 90 days


def available_from(title: Title) -> date | None:
    """The first day a movie is worth searching for, by its minimum availability; None
    when it can be searched already (or nothing is known about its dates)."""
    if title.kind != "movie":
        return None
    meta = title.meta or {}

    def day(key: str) -> date | None:
        v = meta.get(key)
        return date.fromisoformat(v) if v else None

    cinema = day("theatrical_date") or day("release_date")
    mode = title.minimum_availability
    if mode == "announced":
        return None
    if mode == "in_cinemas":
        return cinema
    home = [d for d in (day("digital_date"), day("physical_date")) if d]
    if home:
        return min(home)
    return cinema + RELEASE_FALLBACK if cinema else None


def _series_meta(d: SeriesDetail) -> dict[str, Any]:
    return {
        "original_name": d.original_name,
        "original_language": d.original_language,
        "runtime": d.runtime,
        "overview": d.overview,
        "poster_url": d.poster_url,
        "genres": d.genres,
        "alt_titles": d.alt_titles,
        "ended": d.ended,
    }


def _apply_series(t: Title, d: SeriesDetail) -> None:
    t.name = d.name
    t.sort_name = sort_name(d.name)
    t.year = d.year
    t.status = d.status
    t.imdb_id = d.imdb_id
    t.tvdb_id = d.tvdb_id
    t.meta = _series_meta(d)


def _apply_movie(t: Title, m: MovieDetail) -> None:
    t.name = m.name
    t.sort_name = sort_name(m.name)
    t.year = m.year
    t.status = m.status
    t.imdb_id = m.imdb_id
    t.meta = _movie_meta(m)


def _invalid(field: str, message: str) -> ProblemError:
    return ProblemError(
        422,
        "Validation Failed",
        slug="validation",
        errors=[FieldError(field=field, message=message)],
    )


class MediaService:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        discovery: DiscoveryService,
        profiles: ProfilesService,
    ) -> None:
        self.db = db
        self.bus = bus
        self.discovery = discovery
        self.profiles = profiles

    def register_jobs(self, jobs: JobRunner, scheduler: Scheduler) -> None:
        @jobs.handler(REFRESH_JOB, queue="net", max_attempts=1)
        async def run(ctx: JobContext) -> dict[str, Any]:
            return await self.refresh_stale()

        scheduler.define(
            ScheduleDef(
                "refresh_metadata",
                REFRESH_JOB,
                timedelta(hours=12),
                description="Re-read metadata from TMDB for titles not refreshed in 7 days",
            )
        )

    # ---- titles -----------------------------------------------------------------------------

    async def _check_profile(self, profile_id: str, media: str = "video") -> None:
        try:
            p = await self.profiles.get_profile(profile_id)
        except NotFoundError:
            raise _invalid("body.profile_id", f"no quality profile {profile_id!r}") from None
        if p.media != media:
            what = "an artist" if media == "music" else "a movie or series"
            raise _invalid("body.profile_id", f"{p.name!r} is a {p.media} profile, not for {what}")

    async def _existing(self, s: AsyncSession, kind: str, tmdb_id: int) -> Title | None:
        q = select(Title).where(Title.kind == kind, Title.tmdb_id == tmdb_id)
        return (await s.execute(q)).scalar_one_or_none()

    async def add_movie(
        self,
        tmdb_id: int,
        *,
        profile_id: str,
        monitored: bool,
        actor: Actor,
        search_after: datetime | None = None,
        minimum_availability: str = "released",
        folder: Callable[[], Awaitable[str | None]] | None = None,
        title_folder: str | None = None,
    ) -> Title:
        """``search_after`` None means search now (automatic search picks it up), once
        the movie is available per ``minimum_availability``."""
        if minimum_availability not in AVAILABILITY:
            raise _invalid("body.minimum_availability", f"one of {', '.join(AVAILABILITY)}")
        await self._check_profile(profile_id)
        async with self.db.sessionmaker() as s:
            if (dup := await self._existing(s, "movie", tmdb_id)) is not None:
                raise ConflictError(f"{dup.name} ({dup.year}) is already in the library: {dup.id}")
        detail = await self.discovery.movie(tmdb_id)
        destination_id = await folder() if folder else None  # its own library folder
        title = Title(
            kind="movie",
            tmdb_id=tmdb_id,
            profile_id=profile_id,
            destination_id=destination_id,
            folder=title_folder,
            monitor_mode="all" if monitored else "none",
            minimum_availability=minimum_availability,
            refreshed_at=utcnow(),
        )
        _apply_movie(title, detail)
        async with self.db.sessionmaker() as s:
            s.add(title)
            await s.flush()
            s.add(WantedItem(title_id=title.id, monitored=monitored, next_search_at=search_after))
            try:
                await s.commit()
            except IntegrityError:  # lost a race with a concurrent add
                await s.rollback()
                raise ConflictError(f"{detail.name} is already in the library") from None
        await self._emit("title.added", title, actor)
        return title

    async def add_series(
        self,
        tmdb_id: int,
        *,
        profile_id: str,
        monitor_mode: SeriesMode,
        actor: Actor,
        search_after: datetime | None = None,
        folder: Callable[[], Awaitable[str | None]] | None = None,
        title_folder: str | None = None,
    ) -> Title:
        """A series with every season and episode TMDB lists; one wanted item per episode,
        monitored per ``monitor_mode``."""
        if monitor_mode not in SERIES_MODES:
            raise _invalid("body.monitor_mode", f"one of {', '.join(SERIES_MODES)}")
        await self._check_profile(profile_id)
        async with self.db.sessionmaker() as s:
            if (dup := await self._existing(s, "series", tmdb_id)) is not None:
                raise ConflictError(f"{dup.name} ({dup.year}) is already in the library: {dup.id}")
        detail = await self.discovery.series(tmdb_id)
        destination_id = await folder() if folder else None  # its own library folder
        title = Title(
            kind="series",
            tmdb_id=tmdb_id,
            profile_id=profile_id,
            destination_id=destination_id,
            folder=title_folder,
            monitor_mode=monitor_mode,
            refreshed_at=utcnow(),
        )
        _apply_series(title, detail)
        # Absolute numbers (anime) from TVDB when a key is set; TMDB has none.
        absolute = await self.discovery.absolute_numbers(detail.tvdb_id)
        eps = [Ep(e.season, e.number, e.air_date) for x in detail.seasons for e in x.episodes]
        flags = monitor_initial(monitor_mode, eps, utcnow().date())
        season_flags = seasons_monitored(monitor_mode, flags)
        async with self.db.sessionmaker() as s:
            s.add(title)
            await s.flush()
            for sd in detail.seasons:
                season = Season(
                    title_id=title.id,
                    number=sd.number,
                    monitored=season_flags.get(sd.number, False),
                )
                s.add(season)
                await s.flush()
                for ed in sd.episodes:
                    ep = Episode(
                        season_id=season.id,
                        number=ed.number,
                        air_date=ed.air_date,
                        name=ed.name,
                        absolute_number=absolute.get((sd.number, ed.number)),
                    )
                    s.add(ep)
                    await s.flush()
                    s.add(
                        WantedItem(
                            title_id=title.id,
                            episode_id=ep.id,
                            monitored=flags[(sd.number, ed.number)],
                            next_search_at=search_after,
                        )
                    )
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                raise ConflictError(f"{detail.name} is already in the library") from None
        await self._emit("title.added", title, actor, episodes=len(eps))
        return title

    async def _sync_episodes(
        self,
        s: AsyncSession,
        title: Title,
        detail: SeriesDetail,
        absolute: dict[tuple[int, int], int] | None = None,
    ) -> int:
        """Bring seasons and episodes up to date with TMDB; new episodes get wanted items
        monitored per the series' mode. Returns how many episodes were added."""
        mode: SeriesMode = title.monitor_mode  # type: ignore[assignment]
        seasons = {
            x.number: x
            for x in (await s.execute(select(Season).where(Season.title_id == title.id))).scalars()
        }
        latest = latest_season([*seasons, *(sd.number for sd in detail.seasons)])
        added = 0
        for sd in detail.seasons:
            season = seasons.get(sd.number)
            known = season.monitored if season is not None else None
            if season is None:
                season = Season(
                    title_id=title.id,
                    number=sd.number,
                    monitored=monitor_new(mode, sd.number, None, latest),
                )
                s.add(season)
                await s.flush()
            eps = {
                e.number: e
                for e in (
                    await s.execute(select(Episode).where(Episode.season_id == season.id))
                ).scalars()
            }
            for ed in sd.episodes:
                ep = eps.get(ed.number)
                if ep is not None:
                    ep.name, ep.air_date = ed.name, ed.air_date
                    continue
                ep = Episode(
                    season_id=season.id, number=ed.number, air_date=ed.air_date, name=ed.name
                )
                s.add(ep)
                await s.flush()
                s.add(
                    WantedItem(
                        title_id=title.id,
                        episode_id=ep.id,
                        monitored=monitor_new(mode, sd.number, known, latest),
                    )
                )
                added += 1
        if absolute:
            rows = (
                await s.execute(
                    select(Season.number, Episode)
                    .join(Episode, Episode.season_id == Season.id)
                    .where(Season.title_id == title.id)
                )
            ).all()
            for number, ep in rows:
                ep.absolute_number = absolute.get((number, ep.number), ep.absolute_number)
        return added

    async def episodes(
        self, title_id: str
    ) -> list[tuple[Season, list[tuple[Episode, WantedItem]]]]:
        async with self.db.sessionmaker() as s:
            title = await self._title(s, title_id)
            if title.kind != "series":
                raise ProblemError(409, "Conflict", "only series have episodes", slug="conflict")
            rows = (
                await s.execute(
                    select(Season, Episode, WantedItem)
                    .join(Episode, Episode.season_id == Season.id)
                    .join(WantedItem, WantedItem.episode_id == Episode.id)
                    .where(Season.title_id == title_id)
                    .order_by(Season.number, Episode.number)
                )
            ).all()
            seasons = list(
                (
                    await s.execute(
                        select(Season).where(Season.title_id == title_id).order_by(Season.number)
                    )
                ).scalars()
            )
        by_season: dict[str, list[EpisodeRow]] = {x.id: [] for x in seasons}
        for season, ep, w in rows:
            by_season[season.id].append((ep, w))
        return [(x, by_season[x.id]) for x in seasons]

    async def episode_items(
        self, title_id: str, *, season: int | None = None, item_ids: list[str] | None = None
    ) -> list[tuple[int, Episode, WantedItem]]:
        """(season number, episode, wanted item) of a series, by season or by item."""
        q = (
            select(Season.number, Episode, WantedItem)
            .join(Episode, Episode.season_id == Season.id)
            .join(WantedItem, WantedItem.episode_id == Episode.id)
            .where(Season.title_id == title_id)
            .order_by(Season.number, Episode.number)
        )
        if season is not None:
            q = q.where(Season.number == season)
        if item_ids is not None:
            q = q.where(WantedItem.id.in_(item_ids))
        async with self.db.sessionmaker() as s:
            return [(n, ep, w) for n, ep, w in (await s.execute(q)).all()]

    async def season_info(self, title_id: str) -> dict[int, tuple[int, bool]]:
        """season -> (episode count, finished airing: every episode has aired)."""
        today = utcnow().date()
        out: dict[int, tuple[int, bool]] = {}
        for n, ep, _ in await self.episode_items(title_id):
            count, done = out.get(n, (0, True))
            out[n] = (count + 1, done and ep.air_date is not None and ep.air_date <= today)
        return out

    async def set_season_monitored(
        self, title_id: str, number: int, monitored: bool, *, actor: Actor
    ) -> Season:
        """Monitor or unmonitor a season and every episode in it."""
        async with self.db.sessionmaker() as s:
            title = await self._title(s, title_id)
            season = await s.scalar(
                select(Season).where(Season.title_id == title_id, Season.number == number)
            )
            if season is None:
                raise NotFoundError("season", f"{title_id}/{number}")
            season.monitored = monitored
            ep_ids = select(Episode.id).where(Episode.season_id == season.id)
            await s.execute(
                update(WantedItem)
                .where(WantedItem.episode_id.in_(ep_ids))
                .values(monitored=monitored, version=WantedItem.version + 1)
                .execution_options(synchronize_session=False)
            )
            await s.commit()
        await self._emit(
            "title.updated", title, actor, fields=[f"season {number} monitored"], season=number
        )
        return season

    async def _remonitor_albums(self, s: AsyncSession, title: Title, mode: str) -> None:
        """An artist's new monitor mode, applied to their albums of the chosen kinds."""
        from fetchbox.modules.media.models import Album
        from fetchbox.modules.media.music import DEFAULT_KINDS, monitor_flags

        rows = (
            await s.execute(
                select(Album, WantedItem)
                .join(WantedItem, WantedItem.album_id == Album.id)
                .where(Album.title_id == title.id)
            )
        ).all()
        kinds = (title.meta or {}).get("album_kinds") or DEFAULT_KINDS
        flags = monitor_flags(
            mode, kinds, [(a.id, a.kind, a.release_date) for a, _ in rows], utcnow().date()
        )
        for a, w in rows:
            if w.monitored != flags[a.id]:
                w.monitored = flags[a.id]
                w.version += 1

    async def album_stats(self, title_ids: list[str]) -> dict[str, dict[str, int]]:
        """Per artist: albums, monitored, acquired, missing (monitored, released, no files)."""
        if not title_ids:
            return {}
        from fetchbox.modules.media.models import Album

        today = utcnow().date()
        out = {t: {"albums": 0, "monitored": 0, "acquired": 0, "missing": 0} for t in title_ids}
        async with self.db.sessionmaker() as s:
            rows = (
                await s.execute(
                    select(WantedItem.title_id, WantedItem.monitored, WantedItem.state,
                           Album.release_date)
                    .join(Album, Album.id == WantedItem.album_id)
                    .where(WantedItem.title_id.in_(title_ids))
                )
            ).all()  # fmt: skip
        for tid, monitored, state, released in rows:
            st = out[tid]
            st["albums"] += 1
            st["monitored"] += int(monitored)
            st["acquired"] += int(state == "acquired")
            if monitored and state != "acquired" and released is not None and released <= today:
                st["missing"] += 1
        return out

    async def series_stats(self, title_ids: list[str]) -> dict[str, dict[str, int]]:
        """Per series: episodes, monitored, acquired, missing (monitored, aired, no file)."""
        if not title_ids:
            return {}
        today = utcnow().date()
        out: dict[str, dict[str, int]] = {
            t: {"episodes": 0, "monitored": 0, "acquired": 0, "missing": 0} for t in title_ids
        }
        async with self.db.sessionmaker() as s:
            rows = (
                await s.execute(
                    select(
                        WantedItem.title_id,
                        WantedItem.monitored,
                        WantedItem.state,
                        Episode.air_date,
                        Season.number,
                    )
                    .join(Episode, Episode.id == WantedItem.episode_id)
                    .join(Season, Season.id == Episode.season_id)
                    .where(WantedItem.title_id.in_(title_ids))
                )
            ).all()
        for tid, monitored, state, air, season in rows:
            st = out[tid]
            if season == 0 and not monitored:
                continue  # unmonitored specials don't count
            st["episodes"] += 1
            st["monitored"] += bool(monitored)
            st["acquired"] += state == "acquired"
            if monitored and state != "acquired" and air is not None and air <= today:
                st["missing"] += 1
        return out

    def list_query(self, kind: str | None) -> Select[Any]:
        q = select(Title)
        return q.where(Title.kind == kind) if kind else q

    async def get_title(self, title_id: str) -> Title:
        async with self.db.sessionmaker() as s:
            return await self._title(s, title_id)

    async def _title(self, s: AsyncSession, title_id: str) -> Title:
        row = await s.get(Title, title_id)
        if row is None:
            raise NotFoundError("title", title_id)
        return row

    async def all_titles(self) -> list[Title]:
        async with self.db.sessionmaker() as s:
            return list((await s.execute(select(Title))).scalars())

    async def wanted_for(self, title_ids: list[str]) -> dict[str, list[WantedItem]]:
        if not title_ids:
            return {}
        async with self.db.sessionmaker() as s:
            rows = (
                await s.execute(select(WantedItem).where(WantedItem.title_id.in_(title_ids)))
            ).scalars()
            out: dict[str, list[WantedItem]] = {}
            for w in rows:
                out.setdefault(w.title_id, []).append(w)
        return out

    async def update_title(
        self, title_id: str, changes: dict[str, Any], *, if_match: str | None, actor: Actor
    ) -> Title:
        async with self.db.sessionmaker() as s:
            kind = (await self._title(s, title_id)).kind
        if "profile_id" in changes:
            await self._check_profile(
                changes["profile_id"], "music" if kind == "music" else "video"
            )
        async with self.db.sessionmaker() as s:
            row = await self._title(s, title_id)
            check_if_match(row, if_match)
            mode = changes.get("monitor_mode")
            if mode is not None and row.kind == "movie" and mode not in MOVIE_MONITOR_MODES:
                raise _invalid(
                    "body.monitor_mode",
                    f"a movie's monitor mode is one of {', '.join(MOVIE_MONITOR_MODES)}",
                )
            if (
                mode is not None
                and row.kind == "music"
                and mode not in ("all", "future", "latest", "none")
            ):
                raise _invalid(
                    "body.monitor_mode", "an artist's monitor mode is all, future, latest or none"
                )
            if mode is not None and row.kind == "series" and mode not in SERIES_MODES:
                raise _invalid(
                    "body.monitor_mode",
                    f"a series' monitor mode is one of {', '.join(SERIES_MODES)}",
                )
            # Set every title field before querying: a query autoflushes, and each flush
            # bumps the version (ETag) once.
            if mode is not None:
                row.monitor_mode = mode
            if "profile_id" in changes:
                row.profile_id = changes["profile_id"]
            if "minimum_availability" in changes:
                if row.kind != "movie" or changes["minimum_availability"] not in AVAILABILITY:
                    raise _invalid(
                        "body.minimum_availability",
                        f"movies only; one of {', '.join(AVAILABILITY)}",
                    )
                row.minimum_availability = changes["minimum_availability"]
            if mode is not None and row.kind == "movie":
                items = await s.execute(select(WantedItem).where(WantedItem.title_id == row.id))
                for w in items.scalars():
                    w.monitored = mode == "all"
            elif mode is not None and row.kind == "series":
                await self._remonitor(s, row.id, mode)
            elif mode is not None and row.kind == "music":
                await self._remonitor_albums(s, row, mode)
            await s.commit()
        await self._emit("title.updated", row, actor, fields=sorted(changes))
        return row

    async def refresh_stale(
        self, *, older_than: timedelta = STALE_AFTER, limit: int = REFRESH_BATCH
    ) -> dict[str, Any]:
        """Scheduled refresh: titles not refreshed for ``older_than``, oldest first. One
        failing title doesn't stop the rest; no TMDB key means nothing to do."""
        if self.discovery.config.current.tmdb.api_key is None:
            return {"skipped": "metadata not configured"}
        cutoff = utcnow() - older_than
        async with self.db.sessionmaker() as s:
            ids = list(
                (
                    await s.execute(
                        select(Title.id)
                        .where(
                            Title.tmdb_id.is_not(None),
                            or_(Title.refreshed_at.is_(None), Title.refreshed_at < cutoff),
                        )
                        .order_by(Title.refreshed_at.is_not(None), Title.refreshed_at)
                        .limit(limit)
                    )
                ).scalars()
            )
        done, failed = 0, []
        for tid in ids:
            try:
                await self.refresh_title(tid, actor=SYSTEM, emit=False)
                done += 1
            except (ProblemError, NotFoundError) as exc:
                failed.append(f"{tid}: {exc}")
        return {"refreshed": done, "failed": failed}

    async def _remonitor(self, s: AsyncSession, title_id: str, mode: SeriesMode) -> None:
        """Apply a (new) monitor mode to every season and episode of a series."""
        rows = (
            await s.execute(
                select(Season, Episode, WantedItem)
                .join(Episode, Episode.season_id == Season.id)
                .join(WantedItem, WantedItem.episode_id == Episode.id)
                .where(Season.title_id == title_id)
            )
        ).all()
        eps = [
            Ep(se.number, ep.number, ep.air_date, has_file=w.state == "acquired")
            for se, ep, w in rows
        ]
        flags = monitor_initial(mode, eps, utcnow().date())
        season_flags = seasons_monitored(mode, flags)
        for se, ep, w in rows:
            w.monitored = flags[(se.number, ep.number)]
            se.monitored = season_flags.get(se.number, False)

    async def refresh_title(self, title_id: str, *, actor: Actor, emit: bool = True) -> Title:
        row = await self.get_title(title_id)
        tmdb_id = row.tmdb_id
        if tmdb_id is None:
            raise ProblemError(409, "Conflict", "this title has no TMDB id", slug="conflict")
        added = 0
        if row.kind == "movie":
            movie = await self.discovery.movie(tmdb_id, fresh=True)
            async with self.db.sessionmaker() as s:
                row = await self._title(s, title_id)
                _apply_movie(row, movie)
                row.refreshed_at = utcnow()
                await s.commit()
        else:
            series = await self.discovery.series(tmdb_id, fresh=True)
            # Fetched before the session: no network call while holding a write lock.
            absolute = await self.discovery.absolute_numbers(series.tvdb_id)
            async with self.db.sessionmaker() as s:
                row = await self._title(s, title_id)
                _apply_series(row, series)
                row.refreshed_at = utcnow()
                added = await self._sync_episodes(s, row, series, absolute)
                await s.commit()
        if emit:
            await self._emit("title.refreshed", row, actor, new_episodes=added)
        return row

    async def delete_title(self, title_id: str, *, if_match: str | None, actor: Actor) -> None:
        async with self.db.sessionmaker() as s:
            row = await self._title(s, title_id)
            check_if_match(row, if_match)
            await s.delete(row)  # wanted items cascade in the database
            await s.commit()
        await self._emit("title.removed", row, actor)

    async def _emit(self, type_: str, t: Title, actor: Actor, **data: Any) -> None:
        await self.bus.emit(
            type_,
            subject=Subject(type="title", id=t.id),
            actor=actor,
            name=t.name,
            year=t.year,
            kind=t.kind,
            **data,
        )

    # ---- wanted -----------------------------------------------------------------------------

    def wanted_query(self, state: WantedFilter | None, monitored: bool | None) -> Select[Any]:
        q = select(WantedItem)
        if state == "missing":
            q = q.where(WantedItem.state != "acquired")
        elif state == "cutoff_unmet":
            q = q.where(WantedItem.state == "acquired", WantedItem.cutoff_met.is_(False))
        if monitored is not None:
            q = q.where(WantedItem.monitored.is_(monitored))
        return q

    async def titles_by_id(self, ids: set[str]) -> dict[str, Title]:
        if not ids:
            return {}
        async with self.db.sessionmaker() as s:
            rows = (await s.execute(select(Title).where(Title.id.in_(ids)))).scalars()
            return {t.id: t for t in rows}

    async def episode_info(self, episode_ids: set[str]) -> dict[str, tuple[int, Episode]]:
        """episode id -> (season number, episode)."""
        if not episode_ids:
            return {}
        async with self.db.sessionmaker() as s:
            rows = (
                await s.execute(
                    select(Season.number, Episode)
                    .join(Episode, Episode.season_id == Season.id)
                    .where(Episode.id.in_(episode_ids))
                )
            ).all()
        return {ep.id: (n, ep) for n, ep in rows}

    async def get_wanted(self, wanted_id: str) -> WantedItem:
        async with self.db.sessionmaker() as s:
            row = await s.get(WantedItem, wanted_id)
            if row is None:
                raise NotFoundError("wanted item", wanted_id)
            return row

    async def update_wanted(
        self, wanted_id: str, *, monitored: bool, if_match: str | None, actor: Actor
    ) -> WantedItem:
        async with self.db.sessionmaker() as s:
            row = await s.get(WantedItem, wanted_id)
            if row is None:
                raise NotFoundError("wanted item", wanted_id)
            check_if_match(row, if_match)
            row.monitored = monitored
            title = await self._title(s, row.title_id)
            if title.kind == "movie":  # a movie's monitor mode mirrors its one item
                title.monitor_mode = "all" if monitored else "none"
            await s.commit()
        await self.bus.emit(
            "wanted.updated",
            subject=Subject(type="wanted_item", id=row.id),
            actor=actor,
            name=title.name,
            monitored=monitored,
        )
        return row

    async def mark_searched(self, wanted_id: str, when: datetime) -> None:
        """Runtime state: a raw UPDATE so the item's version (ETag) doesn't change."""
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(WantedItem)
                .where(WantedItem.id == wanted_id)
                .values(last_search_at=when)
                .execution_options(synchronize_session=False)
            )
            await s.commit()

    def history_query(
        self, *, title_id: str | None, wanted_item_id: str | None, type_: str | None
    ) -> Select[Any]:
        q = select(HistoryEvent)
        if title_id:
            q = q.where(HistoryEvent.title_id == title_id)
        if wanted_item_id:
            q = q.where(HistoryEvent.wanted_item_id == wanted_item_id)
        if type_:
            q = q.where(HistoryEvent.type == type_)
        return q


CALENDAR_MAX_DAYS = 100
MOVIE_DATES = (
    ("theatrical_date", "cinema"),
    ("digital_date", "digital"),
    ("physical_date", "disc"),
)


async def calendar(db: Database, start: date, end: date) -> list[dict[str, Any]]:
    """Episodes airing and movie release dates (cinema, digital, disc) from ``start`` to
    ``end`` inclusive, by date then title."""
    out: list[dict[str, Any]] = []
    q = (
        select(Episode, Season.number, Title, WantedItem)
        .join(Season, Season.id == Episode.season_id)
        .join(Title, Title.id == Season.title_id)
        .join(WantedItem, WantedItem.episode_id == Episode.id)
        .where(Episode.air_date >= start, Episode.air_date <= end)
    )
    async with db.sessionmaker() as s:
        for ep, season, title, w in (await s.execute(q)).all():
            out.append(
                {
                    "date": ep.air_date,
                    "kind": "episode",
                    "title_id": title.id,
                    "name": title.name,
                    "poster_url": (title.meta or {}).get("poster_url"),
                    "season": season,
                    "episode": ep.number,
                    "episode_name": ep.name,
                    "release": None,
                    "state": w.state,
                    "monitored": w.monitored,
                }
            )
        mq = (
            select(Title, WantedItem)
            .join(WantedItem, WantedItem.title_id == Title.id)
            .where(Title.kind == "movie")
        )
        movies = (await s.execute(mq)).all()
        from fetchbox.modules.media.models import Album

        albums = (
            await s.execute(
                select(Album, Title, WantedItem)
                .join(Title, Title.id == Album.title_id)
                .join(WantedItem, WantedItem.album_id == Album.id)
                .where(
                    Album.release_date >= start,
                    Album.release_date <= end,
                    WantedItem.monitored.is_(True),
                )
            )
        ).all()
    for album, title, w in albums:
        out.append(
            {
                "date": album.release_date,
                "kind": "album",
                "title_id": title.id,
                "name": title.name,
                "poster_url": (album.meta or {}).get("cover"),
                "season": None,
                "episode": None,
                "episode_name": None,
                "release": None,
                "state": w.state,
                "monitored": w.monitored,
                "album": album.name,
            }
        )
    for title, w in movies:
        meta = title.meta or {}
        for key, label in MOVIE_DATES:
            v = meta.get(key) or (meta.get("release_date") if key == "theatrical_date" else None)
            when = date.fromisoformat(v) if v else None
            if when is None or not start <= when <= end:
                continue
            out.append(
                {
                    "date": when,
                    "kind": "movie",
                    "title_id": title.id,
                    "name": title.name,
                    "poster_url": (title.meta or {}).get("poster_url"),
                    "season": None,
                    "episode": None,
                    "episode_name": None,
                    "release": label,
                    "state": w.state,
                    "monitored": w.monitored,
                }
            )
    out.sort(key=lambda e: (e["date"], e["name"].lower(), e["season"] or 0, e["episode"] or 0))
    return out
