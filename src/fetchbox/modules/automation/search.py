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
from datetime import timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import func, or_, select, update

from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus
from fetchbox.core.jobs import Job, JobContext, JobRunner
from fetchbox.modules.media.models import HistoryEvent, Title, WantedItem
from fetchbox.modules.media.service import MediaService
from fetchbox.modules.profiles.models import QualityProfile
from fetchbox.modules.releases.grabs import BUSY, GrabsService
from fetchbox.modules.releases.service import ReleasesService
from fetchbox.modules.sources.service import SourcesService
from fetchbox.modules.system.config import ConfigService

log = structlog.get_logger(__name__)

SEARCH_JOB = "search.wanted"
SWEEP_INTERVAL = 60.0
BATCH = 10  # searches queued per sweep; the net queue runs a few at a time
Reason = Literal["automatic", "manual"]
SYSTEM = Actor(type="system", id="automation", name="automatic search")


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

    def register_jobs(self) -> None:
        @self.jobs.handler(SEARCH_JOB, queue="net", max_attempts=1)
        async def run(ctx: JobContext) -> dict[str, Any]:
            return await self.search(
                ctx.payload["wanted_item_id"], reason=ctx.payload.get("reason", "automatic")
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
        return (
            select(WantedItem.id)
            .join(Title, Title.id == WantedItem.title_id)
            .join(QualityProfile, QualityProfile.id == Title.profile_id)
            .where(WantedItem.monitored.is_(True), Title.kind == "movie", when, missing | upgrade)
            .order_by(WantedItem.next_search_at.is_not(None), WantedItem.next_search_at)
            .limit(BATCH)
        )

    async def sweep(self) -> int:
        """Queue searches for due items. Nothing happens until an indexer is enabled:
        items stay due, so adding one searches them straight away."""
        if not await self.sources.release_sources():
            return 0
        scheduled = self.config.current.search.automatic
        async with self.db.sessionmaker() as s:
            ids = list((await s.execute(self.due_query(scheduled=scheduled))).scalars())
        n = 0
        for wid in ids:
            if await self.queue(wid, reason="automatic") is not None:
                n += 1
        return n

    async def queue(self, wanted_item_id: str, *, reason: Reason) -> Job | None:
        """None when a search for this item is already pending or running."""
        return await self.jobs.enqueue(
            SEARCH_JOB,
            {"wanted_item_id": wanted_item_id, "reason": reason},
            dedupe_key=f"search:wanted:{wanted_item_id}",
        )

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
        try:
            w = await self.media.get_wanted(wanted_item_id)
        except NotFoundError:
            return {"skipped": "wanted item removed"}
        if (why := self._eligible(w, reason)) is not None:
            return {"skipped": why}
        cfg = self.config.current.search
        upgrade = w.state == "acquired"
        every = cfg.upgrade_every_hours if upgrade else cfg.missing_every_hours
        later = utcnow() + timedelta(hours=every)
        if upgrade and await self.releases.existing(w.id) is None:
            # Locked but its file is gone: surfaced in the library, not silently re-grabbed.
            await self._set(w.id, next_search_at=later)
            return {"skipped": "acquired but the file is missing"}
        if (
            reason == "automatic"
            and w.next_search_at is None
            and (n := await self._failures_in_a_row(w.id)) > cfg.retry_alternatives
        ):
            await self._set(w.id, next_search_at=later)
            return {"skipped": f"{n} failed downloads in a row; next search at the interval"}

        if not upgrade:
            async with self.db.sessionmaker() as s:  # claim: missing -> searching
                r = await s.execute(
                    update(WantedItem)
                    .where(WantedItem.id == w.id, WantedItem.state == "missing")
                    .values(state="searching")
                    .execution_options(synchronize_session=False)
                )
                await s.commit()
            if not r.rowcount:  # type: ignore[attr-defined]
                return {"skipped": "no longer missing"}
        mode = "upgrade" if upgrade else "missing"
        result: dict[str, Any] = {"wanted_item_id": w.id, "mode": mode}
        try:
            _, outcome = await self.releases.run(w, mode="automatic", actor=SYSTEM)
            result |= {
                "results": len(outcome.decisions),
                "accepted": sum(d.accepted for d in outcome.decisions),
                "failed_indexers": [f.source_name for f in outcome.failures],
            }
            for pick in outcome.picks[:1]:
                try:
                    grab, _ = await self.grabs.grab(
                        pick, [w.id], reason="upgrade" if upgrade else "search", actor=SYSTEM
                    )
                except ProblemError as exc:  # e.g. a manual grab got there first
                    result["grab_error"] = exc.problem.detail
                else:
                    result |= {"grabbed": grab.id, "release_title": grab.release_title}
        finally:
            async with self.db.sessionmaker() as s:
                # Not grabbed: back to missing (a grab already moved it on).
                await s.execute(
                    update(WantedItem)
                    .where(WantedItem.id == w.id, WantedItem.state == "searching")
                    .values(state="missing")
                    .execution_options(synchronize_session=False)
                )
                await s.commit()
            await self._set(w.id, next_search_at=later)
        log.info("search.automatic", **result)
        return result
