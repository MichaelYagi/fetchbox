"""Tags held back while an album was seeding, written once the torrent is gone.

A hardlinked track is the same file as the torrent's copy: writing tags into it while the
torrent seeds would change the bytes the torrent shares and break it. So the import
leaves such files as downloaded (``tags_written: false``), and this writes their tags
when the download is removed, by hand or after its seeding goal ("remove" action). A
torrent only paused at its goal could be resumed, so its files are left alone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from fetchbox.core.db import Database
from fetchbox.core.events import Event, EventBus
from fetchbox.core.jobs import JobContext, JobRunner
from fetchbox.modules.library import music_files as mfiles
from fetchbox.modules.library.models import MediaFile, MediaFileItem
from fetchbox.modules.media.models import Album, HistoryEvent, Title, Track, WantedItem
from fetchbox.modules.storage.models import Destination

log = structlog.get_logger(__name__)

TAG_JOB = "music.tags"


class DeferredTags:
    def __init__(self, db: Database, bus: EventBus, jobs: JobRunner) -> None:
        self.db = db
        self.jobs = jobs

        @bus.on("download.removed", "download.seeded")
        async def on_gone(ev: Event) -> None:
            if ev.type == "download.seeded" and ev.data.get("action") != "remove":
                return  # paused: it may seed again
            if ev.subject is not None:
                await self.jobs.enqueue(
                    TAG_JOB, {"download_id": ev.subject.id}, dedupe_key=f"{TAG_JOB}:{ev.subject.id}"
                )

    def register_jobs(self) -> None:
        @self.jobs.handler(TAG_JOB, queue="maintenance", max_attempts=2)
        async def run(ctx: JobContext) -> dict[str, Any]:
            return await self.write_for(ctx.payload["download_id"])

    async def write_for(self, download_id: str) -> dict[str, Any]:
        """Write the deferred tags of the tracks this download brought in."""
        async with self.db.sessionmaker() as s:
            events = (
                await s.execute(
                    select(HistoryEvent.wanted_item_id, HistoryEvent.data).where(
                        HistoryEvent.type.in_(("imported", "upgraded"))
                    )
                )
            ).all()
            wanted = {w for w, d in events if w and (d or {}).get("download_id") == download_id}
            if not wanted:
                return {"tagged": 0}
            rows = (
                await s.execute(
                    select(MediaFile, Track, Album, Title, Destination)
                    .join(MediaFileItem, MediaFileItem.media_file_id == MediaFile.id)
                    .join(WantedItem, WantedItem.id == MediaFileItem.wanted_item_id)
                    .join(Track, Track.id == MediaFile.track_id)
                    .join(Album, Album.id == Track.album_id)
                    .join(Title, Title.id == Album.title_id)
                    .join(Destination, Destination.id == MediaFile.destination_id)
                    .where(WantedItem.id.in_(wanted))
                )
            ).all()
            todo = [r for r in rows if not (r[0].mediainfo or {}).get("tags_written")]
            if not todo:
                return {"tagged": 0}
            totals: dict[tuple[str, int], int] = {}
            discs: dict[str, int] = {}
            for t in (
                await s.execute(select(Track).where(Track.album_id.in_({r[2].id for r in todo})))
            ).scalars():
                totals[(t.album_id, t.disc)] = totals.get((t.album_id, t.disc), 0) + 1
                discs[t.album_id] = max(discs.get(t.album_id, 1), t.disc)
        from fetchbox.modules.library.service import _cover

        covers: dict[str, bytes | None] = {}
        done = 0
        for mf, track, album, title, dest in todo:
            if album.id not in covers:
                covers[album.id] = await _cover(album.meta or {})
            values = mfiles.TagValues(
                artist=title.name,
                album=album.name,
                year=album.release_date.year if album.release_date else None,
                title=track.name,
                track=track.number,
                tracks=totals[(album.id, track.disc)],
                disc=track.disc,
                discs=discs[album.id],
                artist_mbid=title.mbid,
                album_mbid=album.mbid,
                release_mbid=album.edition_mbid,
                recording_mbid=track.recording_mbid,
            )
            path = Path(dest.path) / mf.relative_path
            try:
                await asyncio.to_thread(mfiles.write_tags, path, values, covers[album.id])
            except Exception as exc:  # a missing or unreadable file: leave it for later
                log.warning("music.deferred_tags_failed", path=str(path), error=repr(exc))
                continue
            async with self.db.sessionmaker() as s:
                row = await s.get(MediaFile, mf.id)
                if row is not None:
                    row.mediainfo = {**(row.mediainfo or {}), "tags_written": True}
                    await s.commit()
            done += 1
        return {"tagged": done}
