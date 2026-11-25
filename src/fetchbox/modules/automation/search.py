"""Automatic search: find and grab a release for wanted items without being asked.

One rule drives it: a wanted item is due when ``next_search_at`` is NULL ("search now")
or in the past. Adding a title leaves it NULL (search on add); a failed download sets it
back to NULL (look for an alternative); every search pushes it forward by the configured
interval (``search.missing_every_hours``, ``search.upgrade_every_hours``).

A sweep runs every minute, and at once on ``title.added``, ``download.failed`` and
``source.added``. It queues one ``search.wanted`` job per due item (deduped per item), so
nothing depends on an event arriving: a dropped event only delays the search to the next
sweep. With ``search.automatic`` off, only NULL items (explicit requests) are searched.

Eligible: monitored, not busy (grabbed / downloading / importing), and either not yet
acquired and unlocked, or acquired below the cutoff with upgrades allowed, the file
still there and no manual lock. A locked item whose file is gone is never re-grabbed here (that's
redownload). Manual searches (``POST /wanted/{id}/search``) skip the monitored and lock
checks; the decision engine still only accepts real upgrades for acquired items.

The search grabs the top pick, if any. After ``search.retry_alternatives`` failures in a
row (since the last import), a failed item waits for its regular interval instead.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import func, or_, select, update

from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus
from fetchbox.core.jobs import Job, JobContext, JobRunner
from fetchbox.core.schedules import ScheduleDef, Scheduler
from fetchbox.modules.automation.rss import RSS_JOB, rss_sync
from fetchbox.modules.media.models import (
    Album,
    Episode,
    HistoryEvent,
    Season,
    Title,
    WantedItem,
)
from fetchbox.modules.media.series import aired
from fetchbox.modules.media.service import MediaService, available_from
from fetchbox.modules.profiles.models import QualityProfile
from fetchbox.modules.releases.decision import Candidate
from fetchbox.modules.releases.grabs import BUSY, GrabsService
from fetchbox.modules.releases.grabs import Reason as GrabReason
from fetchbox.modules.releases.service import ReleasesService
from fetchbox.modules.sources.service import SourcesService
from fetchbox.modules.system.config import ConfigService

log = structlog.get_logger(__name__)

SEARCH_JOB = "search.wanted"
SEASON_JOB = "search.season"
EPISODE_FALLBACK = 5  # after a season search, episodes still uncovered searched one by one
SWEEP_INTERVAL = 60.0
BATCH = 10  # searches queued per sweep; the net queue runs a few at a time
Reason = Literal["automatic", "manual"]
SYSTEM = Actor(type="system", id="automation", name="automatic search")


def _search_key(wanted_item_id: str) -> str:
    return f"search:wanted:{wanted_item_id}"


class AutoSearch:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        jobs: JobRunner,
        *,
        releases: ReleasesService,
        grabs: GrabsService,
        media: MediaService,
        sources: SourcesService,
        config: ConfigService,
    ) -> None:
        self.db = db
        self.bus = bus
        self.jobs = jobs
        self.releases = releases
        self.grabs = grabs
        self.media = media
        self.sources = sources
        self.config = config
        self.sweep_interval = SWEEP_INTERVAL
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    # ---- lifecycle ----------------------------------------------------------------------------

    def register_jobs(self, scheduler: Scheduler | None = None) -> None:
        @self.jobs.handler(RSS_JOB, queue="net", max_attempts=1)
        async def run_rss(ctx: JobContext) -> dict[str, Any]:
            return await rss_sync(self)

        if scheduler is not None:
            scheduler.define(
                ScheduleDef(
                    "rss_sync",
                    RSS_JOB,
                    timedelta(minutes=15),
                    description="Read every indexer's newest releases and grab wanted ones",
                )
            )

        @self.jobs.handler(SEARCH_JOB, queue="net", max_attempts=1)
        async def run(ctx: JobContext) -> dict[str, Any]:
            return await self.search(
                ctx.payload["wanted_item_id"], reason=ctx.payload.get("reason", "automatic")
            )

        @self.jobs.handler(SEASON_JOB, queue="net", max_attempts=1)
        async def run_season(ctx: JobContext) -> dict[str, Any]:
            return await self.search_season(
                ctx.payload["title_id"],
                int(ctx.payload["season"]),
                reason=ctx.payload.get("reason", "automatic"),
            )

    async def start(self) -> None:
        await self.recover()
        sub = self.bus.subscribe("title.added", "download.failed", "source.added")

        async def wake() -> None:
            async for _ in sub:
                self._wake.set()

        async def loop() -> None:
            while True:
                try:
                    await self.sweep()
                except Exception:
                    log.exception("search.sweep_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.sweep_interval)
                self._wake.clear()

        self._tasks = [
            asyncio.create_task(wake(), name="search-wake"),
            asyncio.create_task(loop(), name="search-sweep"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks = []

    def wake(self) -> None:
        self._wake.set()

    async def recover(self) -> int:
        """Items left ``searching`` by a crash go back to ``missing`` (and are due)."""
        async with self.db.sessionmaker() as s:
            r = await s.execute(
                update(WantedItem)
                .where(WantedItem.state == "searching")
                .values(state="missing", next_search_at=None)
                .execution_options(synchronize_session=False)
            )
            await s.commit()
        return int(r.rowcount or 0)  # type: ignore[attr-defined]

    # ---- sweep --------------------------------------------------------------------------------

    def due_query(self, *, scheduled: bool) -> Any:
        """(wanted item id, title id, season number or None) of due items: movies, and
        episodes that have aired."""
        now = utcnow()
        when = (
            or_(WantedItem.next_search_at.is_(None), WantedItem.next_search_at <= now)
            if scheduled
            else WantedItem.next_search_at.is_(None)
        )
        missing = (WantedItem.state == "missing") & WantedItem.lock.is_(None)
        upgrade = (
            (WantedItem.state == "acquired")
            & or_(WantedItem.lock.is_(None), WantedItem.lock == "acquired")
            & WantedItem.cutoff_met.is_(False)
            & QualityProfile.upgrade_allowed.is_(True)
        )
        kind_ok = or_(
            Title.kind == "movie",
            (Title.kind == "series") & (Episode.air_date <= now.date()),
            (Title.kind == "music") & (Album.release_date <= now.date()),
        )
        return (
            select(WantedItem.id, WantedItem.title_id, Season.number)
            .join(Title, Title.id == WantedItem.title_id)
            .join(QualityProfile, QualityProfile.id == Title.profile_id)
            .outerjoin(Episode, Episode.id == WantedItem.episode_id)
            .outerjoin(Season, Season.id == Episode.season_id)
            .outerjoin(Album, Album.id == WantedItem.album_id)
            .where(WantedItem.monitored.is_(True), kind_ok, when, missing | upgrade)
            .order_by(WantedItem.next_search_at.is_not(None), WantedItem.next_search_at)
            .limit(BATCH)
        )

    async def sweep(self) -> int:
        """Queue searches for due items: a movie on its own, episodes one job per season.
        Nothing happens until an indexer is enabled: items stay due, so adding one
        searches them straight away."""
        if not await self.sources.release_sources():
            return 0
        scheduled = self.config.current.search.automatic
        async with self.db.sessionmaker() as s:
            rows = list((await s.execute(self.due_query(scheduled=scheduled))).all())
        n = 0
        seasons: set[tuple[str, int]] = set()
        for wid, title_id, season in rows:
            if season is None:
                n += await self.queue(wid, reason="automatic") is not None
            elif (title_id, season) not in seasons:
                seasons.add((title_id, season))
                n += await self.queue_season(title_id, season, reason="automatic") is not None
        return n

    async def queue_season(self, title_id: str, season: int, *, reason: Reason) -> Job | None:
        return await self.jobs.enqueue(
            SEASON_JOB,
            {"title_id": title_id, "season": season, "reason": reason},
            dedupe_key=f"search:season:{title_id}:{season}",
        )

    async def request_title(self, title_id: str) -> Job:
        """A user's "search now" for a title: a movie's item, or every season of a series
        with aired episodes still to get. 409 if nothing could be queued."""
        title = await self.media.get_title(title_id)
        if title.kind == "movie":
            items = (await self.media.wanted_for([title_id])).get(title_id, [])
            if not items:
                raise ConflictError(f"title {title_id} has no wanted items")
            return await self.request(items[0].id)
        if title.kind == "music":
            # every wanted, released album still to get: one search each
            today = utcnow().date()
            async with self.db.sessionmaker() as s:
                ids = list(
                    (
                        await s.execute(
                            select(WantedItem.id)
                            .join(Album, Album.id == WantedItem.album_id)
                            .where(
                                WantedItem.title_id == title_id,
                                WantedItem.monitored.is_(True),
                                WantedItem.state.not_in(("acquired", *BUSY)),
                                Album.release_date <= today,
                            )
                        )
                    ).scalars()
                )
            album_jobs = [await self.queue(wid, reason="manual") for wid in ids]
            first = next((j for j in album_jobs if j is not None), None)
            if first is None:
                raise ConflictError("no released, wanted albums to search for")
            return first
        today = utcnow().date()
        seasons = sorted(
            {
                n
                for n, ep, w in await self.media.episode_items(title_id)
                if w.state not in ("acquired", *BUSY) and aired(ep.air_date, today)
            }
        )
        jobs = [await self.queue_season(title_id, n, reason="manual") for n in seasons]
        queued = [j for j in jobs if j is not None]
        if not queued:
            raise ConflictError(
                "nothing to search for: every aired episode is downloaded or downloading, "
                "or a search is already queued"
            )
        return queued[0]

    async def queue(self, wanted_item_id: str, *, reason: Reason) -> Job | None:
        """None when a search for this item is already pending or running."""
        return await self.jobs.enqueue(
            SEARCH_JOB,
            {"wanted_item_id": wanted_item_id, "reason": reason},
            dedupe_key=_search_key(wanted_item_id),
        )

    async def queued(self, wanted_item_id: str, *, reason: Reason) -> Job | None:
        """Queue a search, or the one already queued for the item (say the scheduled search
        for missing items got there first)."""
        job = await self.queue(wanted_item_id, reason=reason)
        return job or await self.jobs.latest(_search_key(wanted_item_id))

    async def request(self, wanted_item_id: str) -> Job:
        """A user's "search now": 409 while the item is busy or already being searched."""
        w = await self.media.get_wanted(wanted_item_id)
        if w.state in BUSY:
            raise ConflictError(f"already being downloaded ({w.state}): wanted item {w.id}")
        job = await self.queue(w.id, reason="manual")
        if job is None:
            raise ConflictError(f"a search for wanted item {w.id} is already queued")
        return job

    # ---- one search ---------------------------------------------------------------------------

    async def _failures_in_a_row(self, wanted_item_id: str) -> int:
        """``failed`` history events since the item's last import (or unlock)."""
        async with self.db.sessionmaker() as s:
            since = await s.scalar(
                select(func.max(HistoryEvent.created_at)).where(
                    HistoryEvent.wanted_item_id == wanted_item_id,
                    HistoryEvent.type.in_(("imported", "upgraded", "unlocked")),
                )
            )
            q = select(func.count()).where(
                HistoryEvent.wanted_item_id == wanted_item_id, HistoryEvent.type == "failed"
            )
            if since is not None:
                q = q.where(HistoryEvent.created_at > since)
            return int(await s.scalar(q) or 0)

    async def _set(self, wanted_item_id: str, **values: Any) -> None:
        """Runtime state: no version (ETag) bump."""
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(WantedItem)
                .where(WantedItem.id == wanted_item_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            await s.commit()

    def _eligible(self, w: WantedItem, reason: Reason) -> str | None:
        """Why not to search, or None."""
        if w.state in BUSY or w.state == "searching":
            return f"already {w.state}"
        if reason == "manual":
            return None
        if not w.monitored:
            return "not monitored"
        if w.lock == "manual":
            return "locked by the user"
        if w.state == "acquired" and w.cutoff_met:
            return "cutoff met"
        return None

    async def search(self, wanted_item_id: str, *, reason: Reason) -> dict[str, Any]:
        """One movie or one episode."""
        try:
            w = await self.media.get_wanted(wanted_item_id)
        except NotFoundError:
            return {"skipped": "wanted item removed"}
        return await self._search([w], reason=reason, season_query=False)

    async def search_season(self, title_id: str, season: int, *, reason: Reason) -> dict[str, Any]:
        """Every eligible aired episode of a season: one season search (packs first, then
        single episodes for the gaps), then a few episode searches for what's left."""
        try:
            rows = await self.media.episode_items(title_id, season=season)
        except NotFoundError:
            return {"skipped": "title removed"}
        today = utcnow().date()
        items = [w for _, ep, w in rows if aired(ep.air_date, today)]
        if not items:
            return {"skipped": "no aired episodes in this season"}
        return await self._search(items, reason=reason, season_query=True)

    async def _search(
        self,
        candidates: list[WantedItem],
        *,
        reason: Reason,
        season_query: bool,
        feed: list[Candidate] | None = None,
    ) -> dict[str, Any]:
        """Search for ``candidates`` and grab the best, or (``feed``) decide on releases
        already found (RSS): then the items' search schedule is left alone."""
        cfg = self.config.current.search
        now = utcnow()
        later = {
            "missing": now + timedelta(hours=cfg.missing_every_hours),
            "upgrade": now + timedelta(hours=cfg.upgrade_every_hours),
        }

        def mode(w: WantedItem) -> str:
            return "upgrade" if w.state == "acquired" else "missing"

        if reason == "automatic" and candidates:
            title = await self.media.get_title(candidates[0].title_id)
            start = available_from(title)
            if start is not None and start > now.date():
                # Not out yet (by its minimum availability): wait for that day.
                when = datetime.combine(start, time.min, tzinfo=UTC)
                for w in candidates:
                    await self._set(w.id, next_search_at=when)
                return {"skipped": f"not available until {start.isoformat()}"}

        eligible: list[WantedItem] = []
        skipped: list[str] = []
        for w in candidates:
            if (why := self._eligible(w, reason)) is not None:
                skipped.append(why)
                continue
            if mode(w) == "upgrade" and await self.releases.existing(w.id) is None:
                # Locked but its file is gone: surfaced in the library, not silently re-grabbed.
                await self._set(w.id, next_search_at=later["upgrade"])
                skipped.append("acquired but the file is missing")
                continue
            if (
                reason == "automatic"
                and w.next_search_at is None
                and (n := await self._failures_in_a_row(w.id)) > cfg.retry_alternatives
            ):
                await self._set(w.id, next_search_at=later["missing"])
                skipped.append(f"{n} failed downloads in a row; next search at the interval")
                continue
            eligible.append(w)

        # claim: missing -> searching (one that changed meanwhile drops out)
        missing_ids = [w.id for w in eligible if mode(w) == "missing"]
        if missing_ids:
            async with self.db.sessionmaker() as s:
                await s.execute(
                    update(WantedItem)
                    .where(WantedItem.id.in_(missing_ids), WantedItem.state == "missing")
                    .values(state="searching")
                    .execution_options(synchronize_session=False)
                )
                await s.commit()
                claimed = set(
                    (
                        await s.execute(
                            select(WantedItem.id).where(
                                WantedItem.id.in_(missing_ids), WantedItem.state == "searching"
                            )
                        )
                    ).scalars()
                )
            eligible = [w for w in eligible if mode(w) == "upgrade" or w.id in claimed]
            if missing_ids and not claimed and len(candidates) == 1:
                skipped.append("no longer missing")
        if not eligible:
            return {"skipped": skipped[0] if len(skipped) == 1 else "; ".join(sorted(set(skipped)))}

        ids = [w.id for w in eligible]
        result: dict[str, Any] = {
            "mode": mode(eligible[0]) if len(eligible) == 1 else "season",
            "items": len(eligible),
        }
        if len(eligible) == 1:
            result["wanted_item_id"] = eligible[0].id
        grabs: list[tuple[str, str]] = []
        taken: set[str] = set()
        scheduled = False
        try:
            if feed is not None:
                outcome, plan = await self.releases.evaluate(eligible, feed)
                scheduled = True  # RSS doesn't move the next search
            else:
                _, outcome, plan = await self.releases.run(
                    eligible, mode="automatic", actor=SYSTEM, season_query=season_query
                )
                # Before grabbing: a download that fails right away sets next_search_at
                # back to "now" (search for an alternative), which mustn't be overwritten.
                for w in eligible:
                    await self._set(w.id, next_search_at=later[mode(w)])
                scheduled = True
            result |= {
                "results": len(outcome.decisions),
                "accepted": sum(d.accepted for d in outcome.decisions),
                "failed_indexers": [f.source_name for f in outcome.failures],
            }
            for pick in outcome.picks:
                await self._grab(
                    pick, plan.covered(pick), ids, taken, grabs, result,
                    via="rss" if feed is not None else "search", manual=reason == "manual",
                )  # fmt: skip
            if season_query and feed is None:  # episodes the season search didn't cover, one by one
                left = [w for w in eligible if w.id not in taken][:EPISODE_FALLBACK]
                for w in left:
                    _, more, eplan = await self.releases.run([w], mode="automatic", actor=SYSTEM)
                    for pick in more.picks[:1]:
                        await self._grab(pick, eplan.covered(pick), ids, taken, grabs, result,
                                         manual=reason == "manual")  # fmt: skip
        finally:
            async with self.db.sessionmaker() as s:
                # Not grabbed: back to missing (a grab already moved them on).
                await s.execute(
                    update(WantedItem)
                    .where(WantedItem.id.in_(ids), WantedItem.state == "searching")
                    .values(state="missing")
                    .execution_options(synchronize_session=False)
                )
                await s.commit()
            if not scheduled:  # the search itself failed
                for w in eligible:
                    await self._set(w.id, next_search_at=later[mode(w)])
        if grabs:
            result["grabbed"], result["release_title"] = grabs[0]
            if len(grabs) > 1:
                result["grabs"] = [{"grab": g, "release_title": t} for g, t in grabs]
        if feed is None:
            log.info("search.automatic", **result)
        return result

    async def _grab(
        self,
        pick: Any,
        covered: list[str],
        ids: list[str],
        taken: set[str],
        grabs: list[tuple[str, str]],
        result: dict[str, Any],
        *,
        via: GrabReason = "search",
        manual: bool = False,
    ) -> None:
        """Grab ``pick`` for the eligible items it covers that nothing else took yet."""
        mine = [i for i in covered if i in ids and i not in taken]
        if not mine:
            return
        async with self.db.sessionmaker() as s:
            rows = (
                await s.execute(
                    select(WantedItem.id, WantedItem.state, WantedItem.monitored).where(
                        WantedItem.id.in_(mine)
                    )
                )
            ).all()
        if not manual:
            # Looked at again now, not just before the search: unticking "monitored" while
            # the indexers were being searched must stop the grab (it can take seconds).
            dropped = {i for i, _, monitored in rows if not monitored}
            if dropped:
                result["skipped"] = "no longer monitored"
                mine = [i for i in mine if i not in dropped]
                rows = [r for r in rows if r[0] not in dropped]
                if not mine:
                    return
        states = {state for _, state, _ in rows}
        reason: GrabReason = "upgrade" if states == {"acquired"} else via
        try:
            grab, _ = await self.grabs.grab(pick, mine, reason=reason, actor=SYSTEM)
        except ProblemError as exc:  # e.g. a manual grab got there first
            result.setdefault("grab_errors", []).append(exc.problem.detail)
            return
        taken.update(mine)
        grabs.append((grab.id, grab.release_title))
