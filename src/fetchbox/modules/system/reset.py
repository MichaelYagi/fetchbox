"""Delete all data, keep the settings: Settings → "Delete all data".

Data is what Fetchbox has collected or done: titles and their seasons, episodes, albums
and tracks, the wanted list, history, the queue, grabs, the blocklist, the activity log,
jobs. Settings stay: indexers, download clients, library folders, quality profiles and
custom formats, naming, notifications, media servers, users and API keys, the app config.

A backup is made first (a reset can be undone by restoring it). Torrents and direct
downloads are stopped the usual way: an unfinished download's data is deleted, a library
file never is. The media files themselves stay unless asked (then they are deleted like
"delete file from disk": into the folder's recycle folder when it has one).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import delete, select

from fetchbox.core.db import Base, Database
from fetchbox.core.events import Actor, Event, EventBus
from fetchbox.modules.downloads.models import Download
from fetchbox.modules.library.models import MediaFile
from fetchbox.modules.storage.models import Destination
from fetchbox.modules.system.activity import entry_for

if TYPE_CHECKING:
    from fetchbox.modules.downloads.service import DownloadsService
    from fetchbox.modules.library.service import LibraryService
    from fetchbox.modules.system.activity import ActivityWriter
    from fetchbox.modules.system.backups import Backups

log = structlog.get_logger(__name__)

# Deleted (all rows), in the foreign-key order the schema gives.
DATA_TABLES = frozenset(
    {
        "title", "season", "episode", "album", "track", "wanted_item", "history_event",
        "media_file", "media_file_item", "download", "grab", "grab_item", "blocklist",
        "activity", "idempotency_record",
    }
)  # fmt: skip
# Jobs too, except one running right now (its runner owns that row).
JOB_TABLE = "job"


class ResetError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class ResetResult:
    backup: str
    downloads_stopped: int = 0
    files_deleted: int = 0
    not_stopped: list[str] = field(default_factory=list)  # downloads a client refused
    not_deleted: list[str] = field(default_factory=list)  # files that couldn't be deleted


class DataReset:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        *,
        backups: Backups,
        downloads: DownloadsService,
        library: LibraryService,
        activity: ActivityWriter | None = None,
    ) -> None:
        self.db = db
        self.bus = bus
        self.backups = backups
        self.downloads = downloads
        self.library = library
        self.activity = activity
        self._lock = asyncio.Lock()

    async def run(self, *, delete_files: bool, actor: Actor) -> ResetResult:
        if self._lock.locked():
            raise ResetError("a reset is already running")
        async with self._lock:
            return await self._run(delete_files, actor)

    async def _run(self, delete_files: bool, actor: Actor) -> ResetResult:
        from fetchbox.modules.system.backups import BackupError

        try:
            backup = await asyncio.to_thread(self.backups.create, "before-reset")
        except BackupError as exc:  # no backup, no reset
            raise ResetError(f"couldn't make a backup first: {exc.message}") from None
        result = ResetResult(backup=backup.id)

        # 1. Stop every torrent and direct download. Unfinished data goes; a file that's
        #    in the library stays (remove() decides, as for "Remove" in the queue).
        async with self.db.sessionmaker() as s:
            active = (
                await s.execute(
                    select(Download.id, Download.name).where(Download.status != "removed")
                )
            ).all()
        for download_id, name in active:
            try:
                await self.downloads.remove(
                    download_id, delete_data=True, blocklist=False, actor=actor
                )
                result.downloads_stopped += 1
            except Exception as exc:  # an external client that's offline: keep going
                log.warning("reset.download_not_stopped", download=download_id, error=str(exc))
                result.not_stopped.append(name)

        # 2. The media files, only when asked.
        if delete_files:
            async with self.db.sessionmaker() as s:
                files = (
                    await s.execute(
                        select(MediaFile.id, MediaFile.relative_path, Destination.path).join(
                            Destination, Destination.id == MediaFile.destination_id
                        )
                    )
                ).all()
            for file_id, rel, root in files:
                try:
                    await self.library.delete_file(file_id, delete_from_disk=True, actor=actor)
                    result.files_deleted += 1
                    await asyncio.to_thread(_prune, (Path(root) / rel).parent, Path(root))
                except Exception as exc:
                    log.warning("reset.file_not_deleted", file=rel, error=str(exc))
                    result.not_deleted.append(rel)

        # 3. The rows. The activity log is written in the background: let it catch up
        #    first, or entries for what was just stopped would land after the wipe.
        if self.activity is not None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(10):
                    await self.activity.settle()
        from fetchbox.core.jobs import Job

        async with self.db.sessionmaker() as s:
            for table in reversed(Base.metadata.sorted_tables):
                if table.name in DATA_TABLES:
                    await s.execute(delete(table))
            await s.execute(delete(Job).where(Job.status != "running"))
            # The log starts afresh with the reset itself, written here rather than left to
            # the background writer: in the same transaction, it can't be lost or wiped.
            event = Event(
                type="system.data_reset",
                actor=actor,
                data={
                    "backup": result.backup,
                    "downloads": result.downloads_stopped,
                    "files": result.files_deleted,
                    "kept_files": "no" if delete_files else "yes",
                },
            )
            if (entry := entry_for(event)) is not None:
                s.add(entry)
            await s.commit()
        self.downloads.live.clear()
        self.downloads._progress_pct.clear()

        await self.bus.publish(event)  # for live views; the writer merges it (same id)
        log.info(
            "system.data_reset",
            backup=result.backup,
            downloads=result.downloads_stopped,
            files=result.files_deleted,
        )
        return result


def _prune(folder: Path, root: Path) -> None:
    """Remove ``folder`` and its parents while empty, up to ``root``."""
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()  # only if empty
        except OSError:
            break
        folder = folder.parent
