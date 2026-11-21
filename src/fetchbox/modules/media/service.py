"""Titles and wanted items. Phase 1 adds movies; series arrive with the TV pipeline."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
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
from fetchbox.modules.media.models import HistoryEvent, Title, WantedItem
from fetchbox.modules.profiles.service import ProfilesService
from fetchbox.providers.metadata import MovieDetail

MOVIE_MONITOR_MODES = ("all", "none")
REFRESH_JOB = "media.refresh_metadata"
STALE_AFTER = timedelta(days=7)
REFRESH_BATCH = 200
SYSTEM = Actor(type="system", id="metadata-refresh", name="metadata refresh")
WantedFilter = Literal["missing", "cutoff_unmet"]
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
    }


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

    async def _check_profile(self, profile_id: str) -> None:
        try:
            await self.profiles.get_profile(profile_id)
        except NotFoundError:
            raise _invalid("body.profile_id", f"no quality profile {profile_id!r}") from None

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
    ) -> Title:
        """``search_after`` None means search now (automatic search picks it up)."""
        await self._check_profile(profile_id)
        async with self.db.sessionmaker() as s:
            if (dup := await self._existing(s, "movie", tmdb_id)) is not None:
                raise ConflictError(f"{dup.name} ({dup.year}) is already in the library: {dup.id}")
        detail = await self.discovery.movie(tmdb_id)
        title = Title(
            kind="movie",
            tmdb_id=tmdb_id,
            profile_id=profile_id,
            monitor_mode="all" if monitored else "none",
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
        if "profile_id" in changes:
            await self._check_profile(changes["profile_id"])
        async with self.db.sessionmaker() as s:
            row = await self._title(s, title_id)
            check_if_match(row, if_match)
            mode = changes.get("monitor_mode")
            if mode is not None and row.kind == "movie" and mode not in MOVIE_MONITOR_MODES:
                raise _invalid(
                    "body.monitor_mode",
                    f"a movie's monitor mode is one of {', '.join(MOVIE_MONITOR_MODES)}",
                )
            # Set every title field before querying: a query autoflushes, and each flush
            # bumps the version (ETag) once.
            if mode is not None:
                row.monitor_mode = mode
            if "profile_id" in changes:
                row.profile_id = changes["profile_id"]
            if mode is not None and row.kind == "movie":
                items = await s.execute(select(WantedItem).where(WantedItem.title_id == row.id))
                for w in items.scalars():
                    w.monitored = mode == "all"
            await s.commit()
        await self._emit("title.updated", row, actor, fields=sorted(changes))
        return row

    async def refresh_stale(
        self, *, older_than: timedelta = STALE_AFTER, limit: int = REFRESH_BATCH
    ) -> dict[str, Any]:
        """Scheduled refresh: movies not refreshed for ``older_than``, oldest first. One
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
                            Title.kind == "movie",
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

    async def refresh_title(self, title_id: str, *, actor: Actor, emit: bool = True) -> Title:
        row = await self.get_title(title_id)
        if row.kind != "movie" or row.tmdb_id is None:
            raise ProblemError(
                409, "Conflict", "only movies can be refreshed so far", slug="conflict"
            )
        detail = await self.discovery.movie(row.tmdb_id, fresh=True)
        async with self.db.sessionmaker() as s:
            row = await self._title(s, title_id)
            _apply_movie(row, detail)
            row.refreshed_at = utcnow()
            await s.commit()
        if emit:
            await self._emit("title.refreshed", row, actor)
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
