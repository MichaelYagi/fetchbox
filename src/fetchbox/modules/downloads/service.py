"""Downloads: grabs become torrents in a client; a poller keeps their state current.

* Grab -> download: a sweep finds grabs whose wanted items are ``grabbed`` and have no
  download yet, and queues a ``downloads.add`` job for each (deduplicated). The
  ``release.grabbed`` event only wakes the sweep early, so a dropped event or a crash
  between grab and add can't lose a grab.
* The job resolves the release through its indexer, picks a client, adds the torrent
  *paused* and records the download as ``queued``.
* Queue manager: per client, at most ``policy.max_active`` downloads run; the rest
  wait paused, highest ``queue_priority`` first, then oldest.
* Stall detection: a running download with no progress and no peers for
  ``policy.stall_minutes`` goes back to the queue (``attempts`` + 1); after
  ``policy.stall_limit`` stalls it fails.
* A failed download is removed from the client with its data, its release is
  blocklisted, and its wanted items return to ``missing``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import case, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from fetchbox.core.concurrency import check_if_match
from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, FieldError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.ids import new_id
from fetchbox.core.jobs import JobContext, JobRunner
from fetchbox.core.providers import HealthStatus, ProviderRegistry, TestResult
from fetchbox.core.secrets import MASK, SecretBox, mask_settings, open_settings, seal_settings
from fetchbox.modules.downloads.models import ACTIVE, Download, DownloadClient
from fetchbox.modules.media.models import HistoryEvent, Title, WantedItem
from fetchbox.modules.releases.models import BlocklistEntry, Grab, GrabItem
from fetchbox.modules.sources.service import SourcesService
from fetchbox.providers.download_clients import (
    AddOptions,
    ClientTorrent,
    DownloadClientError,
    DownloadClientProvider,
    TransferLimits,
)
from fetchbox.providers.download_clients.builtin import BuiltinClient
from fetchbox.providers.download_clients.direct import DirectClient
from fetchbox.providers.indexers import IndexerError, ReleaseCandidate, TorrentSource

log = structlog.get_logger(__name__)

ADD_JOB = "downloads.add"
ADD_ATTEMPTS = 3
POLL_INTERVAL = 2.0
SWEEP_EVERY = 15  # polls between sweeps when nothing wakes us
CATEGORY = {"movie": "fetchbox-movies", "series": "fetchbox-tv"}
BUILTIN_NAME = "Built-in"
DIRECT_NAME = "Direct downloads"


def _superseded(item_id: Any, created_at: Any) -> Any:
    """EXISTS a later grab for the same wanted item. A grab that failed before reaching
    a client has no download row; once a newer grab claims its items, it must not be
    picked up again."""
    later = aliased(Grab)
    later_item = aliased(GrabItem)
    return (
        exists()
        .where(later_item.grab_id == later.id)
        .where(later_item.wanted_item_id == item_id)
        .where(later.created_at > created_at)
    )


class PathMapping(BaseModel):
    local: str = Field(min_length=1, description="Path as Fetchbox sees it")
    client: str = Field(min_length=1, description="The same path as the client sees it")


class Policy(BaseModel):
    max_active: int = Field(default=3, ge=1, le=50, description="Downloads running at once")
    stall_minutes: int = Field(default=120, ge=1, le=10080)
    stall_limit: int = Field(default=3, ge=1, le=20, description="Stalls before failing")
    save_path: str | None = Field(
        default=None, description="Where torrents download; default <downloads root>/Torrents"
    )
    seed_ratio: float | None = Field(
        default=None, ge=0, description="Stop seeding at this upload ratio; null: no limit"
    )
    seed_minutes: int | None = Field(
        default=None, ge=0, description="Stop seeding after this long; null: no limit"
    )
    seed_action: Literal["remove", "pause"] = Field(
        default="remove",
        description="When a goal is met (whichever first, after import): remove the torrent "
        "and its downloaded data (library files are separate links or copies), or pause it",
    )

    path_mappings: list[PathMapping] = Field(
        default=[],
        description="Where the client sees Fetchbox's folders differently (Docker, another "
        "machine): each maps a Fetchbox path prefix to the client's",
    )

    def to_client(self, path: str) -> str:
        """Fetchbox's path as the client sees it (longest matching prefix wins)."""
        norm = path.replace("\\", "/")
        for m in sorted(self.path_mappings, key=lambda m: len(m.local), reverse=True):
            local = m.local.replace("\\", "/").rstrip("/")
            if norm == local or norm.startswith(local + "/"):
                return m.client.rstrip("/\\") + norm[len(local) :]
        return path

    def seeding_done(self, ratio: float | None, seed_seconds: int | None) -> bool:
        if self.seed_ratio is not None and (ratio or 0.0) >= self.seed_ratio:
            return True
        return self.seed_minutes is not None and (seed_seconds or 0) >= self.seed_minutes * 60


def _invalid(exc: ValidationError, prefix: str) -> ProblemError:
    return ProblemError(
        422,
        "Validation Failed",
        slug="validation",
        errors=[
            FieldError(
                field=".".join(str(p) for p in (prefix, *e["loc"])),
                message=e["msg"].removeprefix("Value error, "),
            )
            for e in exc.errors()
        ],
    )


class DownloadsService:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        box: SecretBox,
        providers: ProviderRegistry,
        jobs: JobRunner,
        sources: SourcesService,
        *,
        downloads_root: Path,
        state_dir: Path,
        isolated: bool = False,
    ) -> None:
        self.db = db
        self.bus = bus
        self.box = box
        self.providers = providers
        self.jobs = jobs
        self.sources = sources
        self.downloads_root = downloads_root
        self.state_dir = state_dir
        self.isolated = isolated
        self.clients: dict[str, DownloadClientProvider] = {}
        self.live: dict[str, ClientTorrent] = {}  # download id -> last client status
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._sub_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()  # one poll/schedule pass at a time
        self._progress_pct: dict[str, int] = {}
        self.poll_interval = POLL_INTERVAL
        self.provider_kwargs: dict[str, Any] = {}  # tests inject a mock transport

    # ---- lifecycle ----------------------------------------------------------------------------

    def register_jobs(self) -> None:
        @self.jobs.handler(ADD_JOB, queue="io", max_attempts=ADD_ATTEMPTS)
        async def add(ctx: JobContext) -> dict[str, Any]:
            return await self._add_job(ctx.payload["grab_id"], final=ctx.attempt >= ADD_ATTEMPTS)

    async def start(self) -> None:
        await self._seed_builtin()
        async with self.db.sessionmaker() as s:
            rows = list((await s.execute(select(DownloadClient))).scalars())
        for row in rows:
            if row.enabled:
                await self._start_client(row)
        sub = self.bus.subscribe("release.grabbed")

        async def wake_on_grab() -> None:
            async for _ in sub:
                self._wake.set()

        self._sub_task = asyncio.create_task(wake_on_grab(), name="downloads-wake")
        self._task = asyncio.create_task(self._loop(), name="downloads-poll")

    async def stop(self) -> None:
        for t in (self._task, self._sub_task):
            if t is not None:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        for c in self.clients.values():
            await c.stop()
        self.clients.clear()

    async def _seed_builtin(self) -> None:
        """The built-in torrent engine and the direct (HTTP) client, once each."""
        seeds: list[tuple[str, str, type[DownloadClientProvider], Policy]] = [
            ("builtin", BUILTIN_NAME, BuiltinClient, Policy()),
            # A direct download has nothing to seed: drop its copy once imported.
            ("direct", DIRECT_NAME, DirectClient, Policy(seed_minutes=0, seed_action="remove")),
        ]
        async with self.db.sessionmaker() as s:
            for kind, name, cls, policy in seeds:
                if await s.scalar(select(DownloadClient.id).where(DownloadClient.kind == kind)):
                    continue
                row = DownloadClient(
                    kind=kind, name=name, priority=1, policy=policy.model_dump(), settings={}
                )
                s.add(row)
                await s.flush()
                row.settings = seal_settings(
                    self.box, cls.Settings(), context=f"download_client:{row.id}"
                )
            await s.commit()

    def _settings(self, row: DownloadClient) -> BaseModel:
        cls = self.providers.get("download_client", row.kind).Settings
        settings = open_settings(self.box, cls, row.settings, context=f"download_client:{row.id}")
        if self.isolated and row.kind == "builtin":
            settings = settings.model_copy(
                update={
                    "listen_interfaces": "127.0.0.1:0",
                    "dht": False,
                    "lsd": False,
                    "upnp": False,
                }
            )
        return settings

    async def _start_client(self, row: DownloadClient) -> None:
        cls = self.providers.get("download_client", row.kind)
        kwargs: dict[str, Any] = {}
        if row.kind == "builtin":
            kwargs["state_dir"] = self.state_dir
        elif row.kind == "direct":
            kwargs["state_dir"] = self.state_dir.parent / "direct"
            kwargs.update(self.provider_kwargs)
        else:
            kwargs.update(self.provider_kwargs)
        client = cls(self._settings(row), **kwargs)
        assert isinstance(client, DownloadClientProvider)
        await client.start()
        self.clients[row.id] = client

    def policy(self, row: DownloadClient) -> Policy:
        return Policy.model_validate(row.policy or {})

    def save_root(self, row: DownloadClient) -> Path:
        p = self.policy(row).save_path
        return Path(p).expanduser() if p else self.downloads_root / "Torrents"

    # ---- grab -> download ---------------------------------------------------------------------

    async def sweep(self) -> int:
        """Queue an add job for every grab still waiting for its download."""
        async with self.db.sessionmaker() as s:
            waiting = (
                select(Grab.id)
                .join(GrabItem, GrabItem.grab_id == Grab.id)
                .join(WantedItem, WantedItem.id == GrabItem.wanted_item_id)
                .where(WantedItem.state == "grabbed")
                .where(~exists().where(Download.grab_id == Grab.id))
                .where(~_superseded(GrabItem.wanted_item_id, Grab.created_at))
                .distinct()
            )
            ids = list((await s.execute(waiting)).scalars())
        n = 0
        for gid in ids:
            if await self.jobs.enqueue(ADD_JOB, {"grab_id": gid}, dedupe_key=f"download:{gid}"):
                n += 1
        return n

    async def _pick_client(
        self, media_kind: str, src: TorrentSource
    ) -> tuple[DownloadClient, DownloadClientProvider]:
        """Torrents go to the best enabled torrent client; direct files to the direct one."""
        direct = src.url is not None
        async with self.db.sessionmaker() as s:
            q = select(DownloadClient).where(DownloadClient.enabled.is_(True))
            rows = sorted((await s.execute(q)).scalars(), key=lambda r: (r.priority, r.name))
        for row in rows:
            kinds = row.media_kinds or []
            if (row.kind == "direct") != direct:
                continue
            if (not kinds or media_kind in kinds) and row.id in self.clients:
                return row, self.clients[row.id]
        what = "direct download client" if direct else "torrent client"
        raise DownloadClientError("unavailable", f"no enabled {what} is running")

    async def _source(self, grab: Grab) -> TorrentSource:
        c = ReleaseCandidate.model_validate(grab.release["candidate"])
        if c.magnet_url:
            return TorrentSource(magnet=c.magnet_url)
        if grab.indexer_id:
            try:
                row = await self.sources.get(grab.indexer_id)
            except NotFoundError:
                row = None
            if row is not None:
                return await (await self.sources.provider(row)).resolve(c)
        if c.download_url and c.download_url.startswith("magnet:"):
            return TorrentSource(magnet=c.download_url)
        raise IndexerError("invalid_response", "the release's indexer is gone; search again")

    async def _add_job(self, grab_id: str, *, final: bool) -> dict[str, Any]:
        async with self.db.sessionmaker() as s:
            grab = await s.get(Grab, grab_id)
            if grab is None:
                return {"skipped": "grab deleted"}
            if await s.scalar(select(Download.id).where(Download.grab_id == grab_id)):
                return {"skipped": "already added"}
            items = await self._items(s, grab_id)
            if not items or all(w.state != "grabbed" for w in items):
                return {"skipped": "items no longer waiting"}
            newer = await s.scalar(
                select(
                    _superseded(GrabItem.wanted_item_id, grab.created_at).where(
                        GrabItem.wanted_item_id.in_([w.id for w in items])
                    )
                )
            )
            if newer:  # a later grab (say, the alternative to this failed one) owns them
                return {"skipped": "superseded by a newer grab"}
            title = await s.get(Title, items[0].title_id)
        media_kind = title.kind if title else "movie"
        try:
            src = await self._source(grab)
            row, client = await self._pick_client(media_kind, src)
            save = self.save_root(row) / CATEGORY[media_kind]
            ref = await client.add(
                src,
                AddOptions(
                    save_path=self.policy(row).to_client(str(save)),
                    category=CATEGORY[media_kind],
                    paused=True,
                ),
            )
        except Exception as exc:
            kind = exc.kind if isinstance(exc, IndexerError | DownloadClientError) else None
            permanent = kind in ("invalid_torrent", "invalid_response", "auth", "config")
            if not final and not permanent:
                raise  # the job runner retries with backoff
            # Final: never leave the items "grabbed", or the sweep would retry forever.
            await self._give_up(grab, items, f"could not add the torrent: {exc}")
            return {"failed": str(exc)}

        now = utcnow()
        async with self.db.sessionmaker() as s:
            existing = await s.scalar(
                select(Download).where(Download.client_id == row.id, Download.client_ref == ref)
            )
            if existing is not None and existing.status in ACTIVE:
                return {"skipped": f"torrent already in the client as download {existing.id}"}
            # A completed download of the same torrent (still seeding; say a redownload
            # without blocklisting) is reused: verified again, then imported again.
            reused = existing is not None and existing.status == "completed"
            d = existing or Download(client_id=row.id, client_ref=ref)
            d.grab_id, d.name, d.media_kind = grab.id, grab.release_title, media_kind
            d.status, d.progress, d.error, d.attempts = "queued", 0.0, None, 0
            d.save_path, d.last_progress_at, d.created_at = str(save), now, now
            d.started_at = d.completed_at = d.removed_at = None
            d.import_status = d.import_error = d.import_retry_at = d.imported_at = None
            d.import_attempts = 0
            if existing is None:
                s.add(d)
            await s.execute(
                update(WantedItem)
                .where(WantedItem.id.in_([w.id for w in items]), WantedItem.state == "grabbed")
                .values(state="downloading", version=WantedItem.version + 1)
                .execution_options(synchronize_session=False)
            )
            await s.commit()
        if reused:
            with contextlib.suppress(DownloadClientError):
                await client.recheck(ref)
        await self._emit("download.queued", d, name=d.name)
        self._wake.set()
        return {"download_id": d.id, "client": row.name, "ref": ref}

    async def _items(self, s: AsyncSession, grab_id: str) -> list[WantedItem]:
        q = (
            select(WantedItem)
            .join(GrabItem, GrabItem.wanted_item_id == WantedItem.id)
            .where(GrabItem.grab_id == grab_id)
        )
        return list((await s.execute(q)).scalars())

    async def _give_up(self, grab: Grab, items: Sequence[WantedItem], reason: str) -> None:
        """A grab that never became a download: block it, return its items to missing."""
        async with self.db.sessionmaker() as s:
            await self._release_items(s, [w.id for w in items], grab, reason)
            await s.commit()
        await self.bus.emit(
            "download.failed",
            subject=Subject(type="grab", id=grab.id),
            name=grab.release_title,
            error=reason,
        )

    async def _release_items(
        self,
        s: AsyncSession,
        item_ids: list[str],
        grab: Grab,
        reason: str,
        *,
        block: bool = True,
        search_soon: bool = True,
    ) -> None:
        """Items go back to wanting a file. ``search_soon`` clears ``next_search_at`` so
        automatic search looks for an alternative release on its next sweep."""
        soon: dict[str, Any] = {"next_search_at": None} if search_soon else {}
        await s.execute(
            update(WantedItem)
            .where(WantedItem.id.in_(item_ids), WantedItem.state.in_(("grabbed", "downloading")))
            # An upgrade's item still has its old file: back to acquired, not missing.
            .values(
                state=case((WantedItem.lock == "acquired", "acquired"), else_="missing"),
                version=WantedItem.version + 1,
                **soon,
            )
            .execution_options(synchronize_session=False)
        )
        title_id = None
        for wid in item_ids:
            w = await s.get(WantedItem, wid)
            if w is None:
                continue
            title_id = w.title_id
            s.add(
                HistoryEvent(
                    wanted_item_id=wid,
                    title_id=w.title_id,
                    type="failed",
                    data={
                        "grab_id": grab.id,
                        "release_title": grab.release_title,
                        "reason": reason,
                    },
                )
            )
        if block:
            s.add(
                BlocklistEntry(
                    title_id=title_id,
                    info_hash=grab.info_hash,
                    guid=grab.release.get("candidate", {}).get("guid"),
                    release_title=grab.release_title,
                    indexer_id=grab.indexer_id,
                    reason=reason,
                )
            )

    # ---- polling, queue, stalls ---------------------------------------------------------------

    async def _loop(self) -> None:
        n = 0
        while True:
            try:
                if self._wake.is_set() or n % SWEEP_EVERY == 0:
                    self._wake.clear()
                    await self.sweep()
                await self.poll()
            except Exception:
                log.exception("downloads.poll_failed")
            n += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)

    async def poll(self) -> None:
        async with self._lock:
            async with self.db.sessionmaker() as s:
                seeding = (Download.status == "completed") & Download.seeded_at.is_(None)
                rows = list(
                    (
                        await s.execute(
                            select(Download).where(Download.status.in_(ACTIVE) | seeding)
                        )
                    ).scalars()
                )
                clients = {c.id: c for c in (await s.execute(select(DownloadClient))).scalars()}
            by_client: dict[str, list[Download]] = {}
            for d in rows:
                by_client.setdefault(d.client_id or "", []).append(d)
            for client_id, downloads in by_client.items():
                client = self.clients.get(client_id)
                row = clients.get(client_id)
                if client is None or row is None:
                    continue
                try:
                    statuses = {
                        t.ref: t for t in await client.status([d.client_ref for d in downloads])
                    }
                except DownloadClientError as exc:
                    log.warning("downloads.client_unavailable", client=row.name, error=exc.message)
                    continue
                for d in downloads:
                    t = statuses.get(d.client_ref)
                    if d.status == "completed":
                        await self._seed(d, t, row, client)
                    else:
                        await self._observe(d, t, row, client)
                await self._schedule(row, client)

    async def _observe(
        self,
        d: Download,
        t: ClientTorrent | None,
        row: DownloadClient,
        client: DownloadClientProvider,
    ) -> None:
        now = utcnow()
        if t is None:
            await self.fail(d.id, "the torrent disappeared from the download client")
            return
        self.live[d.id] = t
        changes: dict[str, Any] = {}
        if t.progress > d.progress + 1e-6:
            changes["last_progress_at"] = now
        changes.update(progress=t.progress, size=t.size, ratio=t.ratio, seed_seconds=t.seed_seconds)
        if t.state == "error":
            await self.fail(d.id, t.error or "download client error")
            return
        event: str | None = None
        if d.status == "downloading" and t.state == "seeding":
            changes.update(
                status="completed", completed_at=now, progress=1.0, import_status="pending"
            )
            event = "download.completed"
        elif d.status == "downloading" and t.state in ("downloading", "metadata"):
            policy = self.policy(row)
            idle = now - (d.last_progress_at or d.started_at or now)
            if t.seeds + t.peers == 0 and idle >= timedelta(minutes=policy.stall_minutes):
                stalls = d.attempts + 1
                if stalls >= policy.stall_limit:
                    await self.fail(d.id, f"stalled {stalls} times with no peers")
                    return
                await client.pause(d.client_ref)
                changes.update(status="queued", attempts=stalls, last_progress_at=now)
                event = "download.stalled"
        await self._update(d.id, changes)
        if event:
            await self._emit(event, d, name=d.name, save_path=d.save_path, attempts=d.attempts)
        pct = int(t.progress * 100)
        if self._progress_pct.get(d.id) != pct and d.status == "downloading":
            self._progress_pct[d.id] = pct
            await self._emit(
                "download.progress", d, progress=t.progress, rate=t.download_rate, eta=t.eta_seconds
            )

    async def _seed(
        self,
        d: Download,
        t: ClientTorrent | None,
        row: DownloadClient,
        client: DownloadClientProvider,
    ) -> None:
        """A completed torrent: keep ratio / seed time current and stop it once the
        policy's seeding goal is met, but never before its import has succeeded."""
        if t is None:  # removed from the client by hand: nothing left to stop
            await self._update(d.id, {"seeded_at": utcnow()})
            self.live.pop(d.id, None)
            return
        self.live[d.id] = t
        if (t.ratio, t.seed_seconds) != (d.ratio, d.seed_seconds):
            await self._update(d.id, {"ratio": t.ratio, "seed_seconds": t.seed_seconds})
        policy = self.policy(row)
        if d.import_status != "imported" or not policy.seeding_done(t.ratio, t.seed_seconds):
            return
        delete_data = policy.seed_action == "remove" and not await self._symlinked(d)
        try:
            if policy.seed_action == "remove":
                await client.remove(d.client_ref, delete_data)
            else:
                await client.pause(d.client_ref)
        except DownloadClientError as exc:
            log.warning("downloads.seed_stop_failed", download=d.id, error=exc.message)
            return
        await self._update(d.id, {"seeded_at": utcnow()})
        if policy.seed_action == "remove":
            self.live.pop(d.id, None)
        await self._emit(
            "download.seeded",
            d,
            name=d.name,
            ratio=round(t.ratio or 0.0, 2),
            hours=round((t.seed_seconds or 0) / 3600, 1),
            action=policy.seed_action,
            deleted_data=delete_data,
        )

    async def _symlinked(self, d: Download) -> bool:
        """Was this download imported as a symlink? Then its data is the library file."""
        async with self.db.sessionmaker() as s:
            ids = [w.id for w in await self._items(s, d.grab_id)]
            rows = (
                await s.execute(
                    select(HistoryEvent.data).where(
                        HistoryEvent.wanted_item_id.in_(ids),
                        HistoryEvent.type.in_(("imported", "upgraded")),
                    )
                )
            ).scalars()
            return any(
                (r or {}).get("download_id") == d.id and (r or {}).get("link_mode") == "symlink"
                for r in rows
            )

    async def _update(self, download_id: str, values: dict[str, Any]) -> None:
        """Runtime state: no version bump, so a user's ETag stays valid across polls."""
        if not values:
            return
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(Download)
                .where(Download.id == download_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            await s.commit()

    async def _schedule(self, row: DownloadClient, client: DownloadClientProvider) -> None:
        async with self.db.sessionmaker() as s:
            q = select(Download).where(
                Download.client_id == row.id, Download.status.in_(("queued", "downloading"))
            )
            ds = list((await s.execute(q)).scalars())
        running = [d for d in ds if d.status == "downloading"]
        waiting = sorted(
            (d for d in ds if d.status == "queued"), key=lambda d: (-d.queue_priority, d.created_at)
        )
        slots = self.policy(row).max_active - len(running)
        for d in waiting[: max(slots, 0)]:
            try:
                await client.resume(d.client_ref)
            except DownloadClientError as exc:
                await self.fail(d.id, exc.message)
                continue
            await self._update(
                d.id,
                {
                    "status": "downloading",
                    "started_at": d.started_at or utcnow(),
                    "last_progress_at": utcnow(),
                },
            )
            await self._emit("download.started", d, name=d.name)

    # ---- user actions -------------------------------------------------------------------------

    async def get(self, download_id: str) -> Download:
        async with self.db.sessionmaker() as s:
            d = await s.get(Download, download_id)
        if d is None:
            raise NotFoundError("download", download_id)
        return d

    async def downloads(self, statuses: Sequence[str] | None) -> list[Download]:
        async with self.db.sessionmaker() as s:
            q = select(Download).order_by(Download.queue_priority.desc(), Download.created_at)
            if statuses:
                q = q.where(Download.status.in_(statuses))
            return list((await s.execute(q)).scalars())

    def _client_for(self, d: Download) -> DownloadClientProvider:
        client = self.clients.get(d.client_id or "")
        if client is None:
            raise ConflictError("the download's client is not running")
        return client

    async def pause(self, download_id: str, *, actor: Actor) -> Download:
        d = await self.get(download_id)
        if d.status not in ("queued", "downloading"):
            raise ConflictError(f"a {d.status} download can't be paused")
        await self._client_for(d).pause(d.client_ref)
        await self._set_status(d, "paused", actor)
        return await self.get(download_id)

    async def resume(self, download_id: str, *, actor: Actor) -> Download:
        d = await self.get(download_id)
        if d.status != "paused":
            raise ConflictError(f"a {d.status} download can't be resumed")
        await self._set_status(d, "queued", actor)  # the queue manager starts it
        self._wake.set()
        return await self.get(download_id)

    async def recheck(self, download_id: str) -> None:
        d = await self.get(download_id)
        await self._client_for(d).recheck(d.client_ref)

    async def _set_status(self, d: Download, status: str, actor: Actor) -> None:
        async with self._lock, self.db.sessionmaker() as s:
            row = await s.get(Download, d.id)
            assert row is not None
            row.status = status
            await s.commit()
        await self._emit(
            f"download.{'paused' if status == 'paused' else 'resumed'}", d, actor=actor, name=d.name
        )

    async def set_priority(
        self, download_id: str, priority: int, *, if_match: str | None, actor: Actor
    ) -> Download:
        async with self.db.sessionmaker() as s:
            row = await s.get(Download, download_id)
            if row is None:
                raise NotFoundError("download", download_id)
            check_if_match(row, if_match)
            row.queue_priority = priority
            await s.commit()
        self._wake.set()
        return await self.get(download_id)

    async def reorder(self, ids: Sequence[str]) -> list[Download]:
        """First id runs first: priorities n..1 in the given order."""
        async with self.db.sessionmaker() as s:
            rows = {
                d.id: d
                for d in (await s.execute(select(Download).where(Download.id.in_(ids)))).scalars()
            }
            if missing := [i for i in ids if i not in rows]:
                raise ProblemError(
                    422,
                    "Validation Failed",
                    slug="validation",
                    errors=[FieldError(field="body.ids", message=f"unknown download(s) {missing}")],
                )
            for n, i in enumerate(reversed(list(dict.fromkeys(ids))), start=1):
                rows[i].queue_priority = n
            await s.commit()
        self._wake.set()
        return await self.downloads(None)

    async def remove(
        self, download_id: str, *, delete_data: bool, blocklist: bool, actor: Actor
    ) -> None:
        d = await self.get(download_id)
        if d.status == "removed":
            return
        client = self.clients.get(d.client_id or "")
        # Under the poll lock: a poll that already listed this download would otherwise
        # find the torrent gone from the client and fail it.
        async with self._lock, self.db.sessionmaker() as s:
            if client is not None:
                with contextlib.suppress(DownloadClientError):
                    await client.remove(d.client_ref, delete_data)
            row = await s.get(Download, d.id)
            grab = await s.get(Grab, d.grab_id)
            assert row is not None and grab is not None
            was_active = row.status in ACTIVE
            row.status, row.removed_at = "removed", utcnow()
            if was_active:  # the items still want a file
                items = [w.id for w in await self._items(s, grab.id)]
                await self._release_items(
                    s, items, grab, "removed by user", block=blocklist, search_soon=False
                )
            await s.commit()
        self.live.pop(d.id, None)
        await self._emit("download.removed", d, actor=actor, name=d.name, delete_data=delete_data)

    async def fail(self, download_id: str, reason: str) -> None:
        d = await self.get(download_id)
        if d.status not in ACTIVE:  # already removed, failed or completed
            return
        client = self.clients.get(d.client_id or "")
        if client is not None:
            with contextlib.suppress(DownloadClientError):
                await client.remove(d.client_ref, True)
        async with self.db.sessionmaker() as s:
            row = await s.get(Download, d.id)
            grab = await s.get(Grab, d.grab_id)
            assert row is not None and grab is not None
            row.status, row.error = "failed", reason
            items = [w.id for w in await self._items(s, grab.id)]
            await self._release_items(s, items, grab, reason)
            await s.commit()
        self.live.pop(d.id, None)
        await self._emit("download.failed", d, name=d.name, error=reason)

    async def pause_all(self, *, actor: Actor) -> int:
        n = 0
        for d in await self.downloads(("queued", "downloading")):
            with contextlib.suppress(DownloadClientError, ConflictError):
                await self.pause(d.id, actor=actor)
                n += 1
        return n

    async def resume_all(self, *, actor: Actor) -> int:
        n = 0
        for d in await self.downloads(("paused",)):
            await self.resume(d.id, actor=actor)
            n += 1
        return n

    # ---- clients ------------------------------------------------------------------------------

    async def client_rows(self) -> list[DownloadClient]:
        async with self.db.sessionmaker() as s:
            q = select(DownloadClient).order_by(DownloadClient.priority, DownloadClient.name)
            return list((await s.execute(q)).scalars())

    async def client_row(self, client_id: str) -> DownloadClient:
        async with self.db.sessionmaker() as s:
            row = await s.get(DownloadClient, client_id)
        if row is None:
            raise NotFoundError("download client", client_id)
        return row

    def masked(self, row: DownloadClient) -> dict[str, Any]:
        cls = self.providers.get("download_client", row.kind).Settings
        return mask_settings(cls, row.settings)

    def kinds(self) -> list[type[DownloadClientProvider]]:
        return [
            c for c in self.providers.of("download_client") if issubclass(c, DownloadClientProvider)
        ]

    async def create_client(self, data: dict[str, Any], *, actor: Actor) -> DownloadClient:
        """An external client. Its connection is tested; a failure is reported, not fatal
        (the client may simply be off right now)."""
        kind = data["kind"]
        if kind in ("builtin", "direct"):
            raise ConflictError(f"the built-in {kind} client exists already; there is only one")
        try:
            cls = self.providers.get("download_client", kind)
        except LookupError:
            known = ", ".join(c.kind for c in self.kinds() if c.kind != "builtin")
            raise ProblemError(
                422,
                "Validation Failed",
                slug="validation",
                errors=[FieldError(field="body.kind", message=f"unknown kind; known: {known}")],
            ) from None
        try:
            model = cls.Settings.model_validate(data.get("settings") or {})
            policy = Policy.model_validate(data.get("policy") or {})
        except ValidationError as exc:
            raise _invalid(exc, "body") from None
        row = DownloadClient(
            id=new_id(),
            kind=kind,
            name=data["name"],
            enabled=data.get("enabled", True),
            priority=data.get("priority", 1),
            media_kinds=data.get("media_kinds") or [],
            tags=data.get("tags") or [],
            policy=policy.model_dump(),
        )
        row.settings = seal_settings(self.box, model, context=f"download_client:{row.id}")
        async with self.db.sessionmaker() as s:
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                raise ConflictError(f"a download client named {row.name!r} exists") from None
        if row.enabled:
            await self._start_client(row)
        await self.bus.emit(
            "download_client.added",
            subject=Subject(type="download_client", id=row.id),
            actor=actor,
            name=row.name,
            kind=kind,
        )
        self._wake.set()
        return row

    async def delete_client(self, client_id: str, *, if_match: str | None, actor: Actor) -> None:
        async with self.db.sessionmaker() as s:
            row = await s.get(DownloadClient, client_id)
            if row is None:
                raise NotFoundError("download client", client_id)
            check_if_match(row, if_match)
            if row.kind in ("builtin", "direct"):
                raise ConflictError(f"{row.name} is built in; disable it instead")
            busy = await s.scalar(
                select(Download.id).where(
                    Download.client_id == row.id,
                    Download.status.in_(ACTIVE) | (Download.status == "completed"),
                    Download.seeded_at.is_(None),
                )
            )
            if busy:
                raise ConflictError(
                    f"{row.name} still has downloads; remove them or let them finish first"
                )
            await s.delete(row)  # finished downloads keep their rows, client_id NULL
            await s.commit()
        if (c := self.clients.pop(client_id, None)) is not None:
            await c.stop()
        await self.bus.emit(
            "download_client.removed",
            subject=Subject(type="download_client", id=client_id),
            actor=actor,
            name=row.name,
        )

    async def update_client(
        self, client_id: str, changes: dict[str, Any], *, if_match: str | None, actor: Actor
    ) -> DownloadClient:
        async with self.db.sessionmaker() as s:
            row = await s.get(DownloadClient, client_id)
            if row is None:
                raise NotFoundError("download client", client_id)
            check_if_match(row, if_match)
            restart = False
            if "settings" in changes:
                cls = self.providers.get("download_client", row.kind).Settings
                current = open_settings(
                    self.box, cls, row.settings, context=f"download_client:{row.id}"
                ).model_dump(mode="python")
                for k, v in list(changes["settings"].items()):  # MASK sent back = keep it
                    if v == MASK:
                        changes["settings"][k] = current.get(k)
                try:
                    model = cls.model_validate({**current, **changes["settings"]})
                except ValidationError as exc:
                    raise _invalid(exc, "body.settings") from None
                row.settings = seal_settings(self.box, model, context=f"download_client:{row.id}")
                restart = True
            if "policy" in changes:
                try:
                    row.policy = Policy.model_validate(
                        {**(row.policy or {}), **changes["policy"]}
                    ).model_dump()
                except ValidationError as exc:
                    raise _invalid(exc, "body.policy") from None
            for f in ("enabled", "priority", "media_kinds", "tags"):
                if f in changes:
                    setattr(row, f, changes[f])
                    restart = restart or f == "enabled"
            await s.commit()
        if restart:
            old = self.clients.pop(row.id, None)
            if old is not None:
                await old.stop()
            if row.enabled:
                await self._start_client(row)
        await self.bus.emit(
            "download_client.updated",
            subject=Subject(type="download_client", id=row.id),
            actor=actor,
            name=row.name,
            fields=sorted(changes),
        )
        self._wake.set()
        return row

    async def test_client(self, client_id: str) -> TestResult:
        await self.client_row(client_id)
        client = self.clients.get(client_id)
        if client is None:
            return TestResult(ok=False, message="this client is disabled or not running")
        return await client.test()

    async def set_limits(self, limits: TransferLimits, *, actor: Actor) -> TransferLimits:
        for row in await self.client_rows():
            if row.kind == "builtin":
                await self.update_client(
                    row.id,
                    {
                        "settings": {
                            "download_limit": limits.download,
                            "upload_limit": limits.upload,
                        }
                    },
                    if_match=None,
                    actor=actor,
                )
        return await self.limits()

    async def limits(self) -> TransferLimits:
        for row in await self.client_rows():
            if row.kind == "builtin":
                s = self._settings(row)
                return TransferLimits(
                    getattr(s, "download_limit", None), getattr(s, "upload_limit", None)
                )
        return TransferLimits()

    async def health(self) -> HealthStatus:
        rows = [r for r in await self.client_rows() if r.enabled]
        if not any(r.kind != "direct" for r in rows):
            return HealthStatus(status="error", message="No torrent client is enabled")
        problems = []
        for r in rows:
            c = self.clients.get(r.id)
            res = await c.test() if c else TestResult(ok=False, message="not running")
            if not res.ok:
                problems.append(f"{r.name}: {res.message}")
        if problems:
            return HealthStatus(status="error", message="; ".join(problems))
        builtin = next((c for c in self.clients.values() if isinstance(c, BuiltinClient)), None)
        msg = f"listening on port {builtin.listen_port}" if builtin else f"{len(rows)} client(s)"
        return HealthStatus(status="ok", message=msg)

    async def _emit(
        self, type_: str, d: Download, *, actor: Actor | None = None, **data: Any
    ) -> None:
        await self.bus.emit(
            type_, subject=Subject(type="download", id=d.id), actor=actor or Actor(), **data
        )
