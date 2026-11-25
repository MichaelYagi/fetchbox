"""Unlock and redownload: the explicit ways to let automation touch an acquired item.

An import locks its items (``lock = acquired``), so nothing re-grabs them silently.

* ``unlock`` clears the lock (or a manual one), writes an ``unlocked`` history event,
  optionally deletes the current file (to the recycle folder), and queues a search.
  With the file kept, that search only grabs a real upgrade of it; with no file left,
  the item is ``missing`` again.
* ``redownload`` is unlock + optionally blocklist the release the file came from +
  optionally delete the file, and the item is ``missing`` either way: the search judges
  releases on their own, not against the kept file, which the new import replaces.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from fetchbox.core.db import Database
from fetchbox.core.errors import ConflictError, NotFoundError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.modules.automation.search import AutoSearch
from fetchbox.modules.downloads.models import Download
from fetchbox.modules.library.models import MediaFile, MediaFileItem
from fetchbox.modules.library.service import LibraryService
from fetchbox.modules.media.models import HistoryEvent, Title, WantedItem
from fetchbox.modules.releases.grabs import BUSY, GrabsService
from fetchbox.modules.releases.models import Grab


class UnlockService:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        *,
        library: LibraryService,
        grabs: GrabsService,
        autosearch: AutoSearch,
    ) -> None:
        self.db = db
        self.bus = bus
        self.library = library
        self.grabs = grabs
        self.autosearch = autosearch

    async def _file_ids(self, wanted_item_id: str) -> list[str]:
        async with self.db.sessionmaker() as s:
            q = (
                select(MediaFile.id)
                .join(MediaFileItem, MediaFileItem.media_file_id == MediaFile.id)
                .where(MediaFileItem.wanted_item_id == wanted_item_id)
            )
            return list((await s.execute(q)).scalars())

    async def current_grab(self, wanted_item_id: str) -> Grab | None:
        """The grab the item's file was imported from (its latest import)."""
        async with self.db.sessionmaker() as s:
            ev = await s.scalar(
                select(HistoryEvent)
                .where(
                    HistoryEvent.wanted_item_id == wanted_item_id,
                    HistoryEvent.type.in_(("imported", "upgraded")),
                )
                .order_by(HistoryEvent.created_at.desc(), HistoryEvent.id.desc())
                .limit(1)
            )
            download_id = (ev.data or {}).get("download_id") if ev is not None else None
            d = await s.get(Download, download_id) if download_id else None
            return await s.get(Grab, d.grab_id) if d is not None else None

    async def unlock(
        self,
        wanted_item_id: str,
        *,
        delete_file: bool,
        blocklist: bool = False,
        redownload: bool = False,
        actor: Actor,
    ) -> tuple[WantedItem, str | None]:
        """Returns (item, the search job: the one queued, or one already queued for it)."""
        async with self.db.sessionmaker() as s:
            w = await s.get(WantedItem, wanted_item_id)
        if w is None:
            raise NotFoundError("wanted item", wanted_item_id)
        if w.state in BUSY or w.state == "searching":
            raise ConflictError(
                f"wanted item {w.id} is {w.state}; remove its download or wait for it first"
            )

        blocked = None
        if blocklist and (g := await self.current_grab(w.id)) is not None:
            await self.grabs.block(
                title_id=w.title_id,
                info_hash=g.info_hash,
                guid=(g.release.get("candidate") or {}).get("guid"),
                release_title=g.release_title,
                indexer_id=g.indexer_id,
                reason="redownload",
                actor=actor,
            )
            blocked = g.release_title

        deleted: list[str] = []
        if delete_file:
            for fid in await self._file_ids(w.id):
                await self.library.delete_file(fid, delete_from_disk=True, actor=actor)
                deleted.append(fid)
        has_file = bool(await self._file_ids(w.id))

        values: dict[str, Any] = {"lock": None, "next_search_at": None}
        if redownload or not has_file:
            values |= {"state": "missing", "cutoff_met": False}
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(WantedItem)
                .where(WantedItem.id == w.id)
                .values(**values, version=WantedItem.version + 1)
                .execution_options(synchronize_session=False)
            )
            s.add(
                HistoryEvent(
                    wanted_item_id=w.id,
                    title_id=w.title_id,
                    type="unlocked",
                    data={
                        "previous_lock": w.lock,
                        "redownload": redownload,
                        "deleted_files": deleted,
                        "blocklisted": blocked,
                        "actor": actor.name or actor.id or actor.type,
                    },
                )
            )
            await s.commit()
            row = await s.get(WantedItem, w.id, populate_existing=True)
            title = await s.get(Title, w.title_id)
        assert row is not None
        await self.bus.emit(
            "wanted.redownload" if redownload else "wanted.unlocked",
            subject=Subject(type="wanted_item", id=w.id),
            actor=actor,
            name=title.name if title else "?",
            deleted_files=len(deleted),
            blocklisted=blocked,
        )
        job = await self.autosearch.queued(w.id, reason="manual")
        return row, job.id if job is not None else None
