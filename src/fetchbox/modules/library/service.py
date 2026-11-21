"""Library: naming templates, imports, and the files in destinations.

Import of a completed download (``library.import`` job, queued by a sweep that
``download.completed`` wakes, like grabs -> downloads):

1. The grab's wanted items must still be waiting for it (``downloading``).
2. Pick the video: video extensions only, no samples, the largest file for a movie.
   Its size on disk must equal the torrent's size for it (a short file is retried).
3. The destination (the title's, else the default for its kind) must be online.
4. The path comes from the destination's naming template; the file is placed with the
   destination's link mode in two phases (``*.fetchbox-partial``, verify, rename).
5. ``media_file`` is written, the items become ``acquired`` with ``lock = acquired``
   and ``cutoff_met`` from the profile, the replaced file (an upgrade) goes to the
   recycle folder, and history records ``imported`` or ``upgraded``.

Failures leave the download alone and mark the import ``needs_attention`` with the
reason; it is retried on a backoff (1, 5, 15, 60 minutes, then hourly).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fetchbox.core.concurrency import check_if_match
from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, FieldError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.jobs import JobContext, JobRunner
from fetchbox.modules.downloads.models import Download
from fetchbox.modules.downloads.service import DownloadsService
from fetchbox.modules.library.models import MediaFile, MediaFileItem, NamingTemplate
from fetchbox.modules.library.naming import (
    DEFAULT_PATTERNS,
    TemplateError,
    movie_path,
    validate_patterns,
)
from fetchbox.modules.media.models import HistoryEvent, Title, WantedItem
from fetchbox.modules.profiles.service import ProfilesService, to_spec
from fetchbox.modules.releases.models import Grab, GrabItem
from fetchbox.modules.releases.parser import ParsedRelease, parse_release
from fetchbox.modules.storage.models import Destination
from fetchbox.modules.storage.service import RECYCLE_DIR, StorageService
from fetchbox.providers.download_clients import ClientFile, DownloadClientError
from fetchbox.providers.storage import StorageError
from fetchbox.providers.storage.local import quick_hash

log = structlog.get_logger(__name__)

IMPORT_JOB = "library.import"
DEFAULT_TEMPLATE = "Plex / Jellyfin"
VIDEO_EXTS = {"mkv", "mp4", "m4v", "avi", "mov", "wmv", "ts", "m2ts", "webm", "mpg", "mpeg"}
_SAMPLE = re.compile(r"(?<![a-z])sample(?![a-z])", re.I)
RETRY = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1))
SWEEP_INTERVAL = 30.0
CODEC_NAMES = {
    "h264": "x264",
    "h265": "x265",
    "av1": "AV1",
    "vc1": "VC-1",
    "mpeg2": "MPEG2",
    "xvid": "XviD",
    "divx": "DivX",
    "vp9": "VP9",
}
SOURCE_NAMES = {
    "webdl": "WEB-DL",
    "webrip": "WEBRip",
    "bluray": "BluRay",
    "hdtv": "HDTV",
    "dvd": "DVD",
    "sdtv": "SDTV",
    "cam": "CAM",
    "telesync": "TS",
}


def _compare(src: Path, dst: Path) -> str:
    """absent | linked (same inode) | same (same size and quick hash) | different."""
    if not dst.exists():
        return "absent"
    a, b = src.stat(), dst.stat()
    if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
        return "linked"
    if a.st_size == b.st_size and quick_hash(src) == quick_hash(dst):
        return "same"
    return "different"


def _size(p: Path) -> int | None:
    try:
        return p.stat().st_size
    except OSError:
        return None


def _scan(save_path: str, name: str) -> list[ClientFile]:
    """Files of a download as found on disk (when the client no longer knows it)."""
    base = Path(save_path)
    root = base / name
    if root.is_file():
        return [ClientFile(path=name, size=root.stat().st_size, progress=1.0)]
    if root.is_dir():
        return [
            ClientFile(path=str(p.relative_to(base)), size=p.stat().st_size, progress=1.0)
            for p in root.rglob("*")
            if p.is_file()
        ]
    return []


class ImportProblem(Exception):
    """An import that can't proceed now; ``retry`` False means it never will."""

    def __init__(self, message: str, *, retry: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.retry = retry


def pick_video(files: list[ClientFile], *, kind: str = "movie") -> ClientFile | None:
    videos = [
        f
        for f in files
        if PurePosixPath(f.path.replace("\\", "/")).suffix.lower().lstrip(".") in VIDEO_EXTS
        and not any(_SAMPLE.search(part) for part in PurePosixPath(f.path.replace("\\", "/")).parts)
    ]
    if not videos:
        return None
    return max(videos, key=lambda f: f.size)


def movie_context(title: Title, parsed: ParsedRelease, quality: str) -> dict[str, Any]:
    tags = [
        t for t in ("PROPER" if parsed.proper else None, "REPACK" if parsed.repack else None) if t
    ]
    audio = " ".join(
        x
        for x in (
            parsed.audio_codecs[0].upper() if parsed.audio_codecs else None,
            parsed.audio_channels,
            "Atmos" if parsed.atmos else None,
        )
        if x
    )
    return {
        "Title": title.name,
        "Year": title.year,
        "TmdbId": title.tmdb_id,
        "ImdbId": title.imdb_id,
        "Edition": parsed.edition,
        "Quality": quality,
        "Resolution": f"{parsed.resolution}p" if parsed.resolution else None,
        "Source": SOURCE_NAMES.get(parsed.source or "", parsed.source),
        "HDR": parsed.hdr,
        "Codec": CODEC_NAMES.get(parsed.video_codec or "", parsed.video_codec),
        "Audio": audio or None,
        "Group": parsed.group,
        "Proper": " ".join(tags) or None,
        "OriginalName": parsed.raw,
    }


def _quality_json(parsed: ParsedRelease, quality: str) -> dict[str, Any]:
    return {
        "name": quality,
        "source": parsed.source,
        "resolution": parsed.resolution,
        "modifier": parsed.modifier,
        "revision": parsed.revision.version,
        "video_codec": parsed.video_codec,
        "hdr": parsed.hdr,
        "audio_codecs": parsed.audio_codecs,
    }


class LibraryService:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        jobs: JobRunner,
        storage: StorageService,
        downloads: DownloadsService,
        profiles: ProfilesService,
    ) -> None:
        self.db = db
        self.bus = bus
        self.jobs = jobs
        self.storage = storage
        self.downloads = downloads
        self.profiles = profiles
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self.sweep_interval = SWEEP_INTERVAL

    # ---- lifecycle ----------------------------------------------------------------------------

    def register_jobs(self) -> None:
        @self.jobs.handler(IMPORT_JOB, queue="io", max_attempts=1)
        async def run(ctx: JobContext) -> dict[str, Any]:
            return await self.import_download(ctx.payload["download_id"])

    async def seed(self) -> str:
        """The default naming template (created while none exist); returns its id."""
        async with self.db.sessionmaker() as s:
            row = await s.scalar(select(NamingTemplate).order_by(NamingTemplate.created_at))
            if row is None:
                row = NamingTemplate(name=DEFAULT_TEMPLATE, patterns=dict(DEFAULT_PATTERNS))
                s.add(row)
                await s.commit()
            return row.id

    async def start(self) -> None:
        sub = self.bus.subscribe("download.completed", "storage.online")

        async def wake() -> None:
            async for _ in sub:
                self._wake.set()

        async def loop() -> None:
            while True:
                try:
                    await self.sweep()
                except Exception:
                    log.exception("library.sweep_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.sweep_interval)
                self._wake.clear()

        self._tasks = [
            asyncio.create_task(wake(), name="library-wake"),
            asyncio.create_task(loop(), name="library-sweep"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def sweep(self) -> int:
        """Queue imports for completed downloads that are pending or due a retry."""
        now = utcnow()
        async with self.db.sessionmaker() as s:
            due = (Download.import_status == "needs_attention") & (Download.import_retry_at <= now)
            q = select(Download.id).where(
                Download.status == "completed", or_(Download.import_status == "pending", due)
            )
            ids = list((await s.execute(q)).scalars())
        n = 0
        for did in ids:
            if await self.jobs.enqueue(
                IMPORT_JOB, {"download_id": did}, dedupe_key=f"import:{did}"
            ):
                n += 1
        return n

    def wake(self) -> None:
        self._wake.set()

    # ---- import -------------------------------------------------------------------------------

    async def _set(self, download_id: str, **values: Any) -> None:
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(Download)
                .where(Download.id == download_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            await s.commit()

    async def import_download(self, download_id: str) -> dict[str, Any]:
        async with self.db.sessionmaker() as s:
            d = await s.get(Download, download_id)
        if d is None or d.status != "completed" or d.import_status == "imported":
            return {"skipped": "nothing to import"}
        await self._set(download_id, import_status="importing")
        await self.bus.emit(
            "import.started", subject=Subject(type="download", id=d.id), name=d.name
        )
        try:
            result = await self._import(d)
        except ImportProblem as exc:
            attempts = d.import_attempts + 1
            retry_at = utcnow() + RETRY[min(attempts - 1, len(RETRY) - 1)] if exc.retry else None
            await self._set(
                download_id,
                import_status="needs_attention" if exc.retry else "skipped",
                import_error=exc.message,
                import_attempts=attempts,
                import_retry_at=retry_at,
            )
            await self.bus.emit(
                "import.failed",
                subject=Subject(type="download", id=d.id),
                name=d.name,
                error=exc.message,
                retry_at=retry_at.isoformat() if retry_at else None,
            )
            return {"failed": exc.message}
        await self._set(
            download_id, import_status="imported", import_error=None, imported_at=utcnow()
        )
        return result

    async def _items(self, s: AsyncSession, grab_id: str) -> list[WantedItem]:
        q = (
            select(WantedItem)
            .join(GrabItem, GrabItem.wanted_item_id == WantedItem.id)
            .where(GrabItem.grab_id == grab_id)
        )
        return list((await s.execute(q)).scalars())

    async def _files(self, d: Download) -> list[ClientFile]:
        client = self.downloads.clients.get(d.client_id)
        if client is not None:
            with contextlib.suppress(DownloadClientError):
                return await client.files(d.client_ref)
        # Client gone or torrent removed: fall back to what is on disk under its folder.
        return await asyncio.to_thread(_scan, d.save_path or "", d.name)

    async def _import(self, d: Download) -> dict[str, Any]:
        async with self.db.sessionmaker() as s:
            grab = await s.get(Grab, d.grab_id)
            if grab is None:
                raise ImportProblem("the grab for this download is gone", retry=False)
            items = await self._items(s, grab.id)
            if not items:
                raise ImportProblem("no wanted items are linked to this download", retry=False)
            title = await s.get(Title, items[0].title_id)
        if title is None:
            raise ImportProblem("the title was removed", retry=False)
        waiting = [w for w in items if w.state in ("downloading", "importing")]
        if not waiting:
            raise ImportProblem(
                f"the wanted item no longer waits for this download (it is {items[0].state})",
                retry=False,
            )
        if title.kind != "movie":
            raise ImportProblem("importing episodes arrives with the TV pipeline", retry=False)

        # 2. the video file
        video = pick_video(await self._files(d))
        if video is None:
            raise ImportProblem("no video file in the download (samples are ignored)", retry=False)
        src = Path(d.save_path or "") / video.path
        on_disk = await asyncio.to_thread(_size, src)
        if on_disk is None:
            raise ImportProblem(f"{video.path} is missing from {d.save_path}")
        if on_disk != video.size:
            raise ImportProblem(
                f"{video.path} is incomplete on disk ({on_disk} of {video.size} bytes)"
            )

        # 3. destination
        dest = await (
            self.storage.destination(title.destination_id)
            if title.destination_id
            else self.storage.default_destination(title.kind)
        )
        dest = await self.storage.check(dest.id)
        if dest.status not in ("online", "degraded"):
            raise ImportProblem(f"destination {dest.name} is {dest.status}: {dest.status_message}")

        # 4. path and placement
        release = grab.release
        parsed = (
            ParsedRelease.model_validate(release["parsed"])
            if release.get("parsed")
            else parse_release(grab.release_title)
        )
        quality = release.get("quality") or parsed.quality
        patterns = await self._patterns(dest)
        rel = movie_path(patterns, movie_context(title, parsed, quality), src.suffix)
        dst = Path(dest.path) / rel
        mode = await self._place(src, dst, dest)

        # 5. records
        qh = await asyncio.to_thread(quick_hash, dst)
        st = await asyncio.to_thread(dst.stat)
        profile = to_spec(await self.profiles.get_profile(title.profile_id))
        score = int(release.get("format_score") or 0)
        tier = profile.tier_index(quality)
        cutoff_met = (
            tier is not None
            and tier >= profile.cutoff_index
            and (profile.cutoff_format_score is None or score >= profile.cutoff_format_score)
        )
        replaced: list[str] = []
        async with self.db.sessionmaker() as s:
            ids = [w.id for w in waiting]
            old_q = (
                select(MediaFile)
                .join(MediaFileItem, MediaFileItem.media_file_id == MediaFile.id)
                .where(MediaFileItem.wanted_item_id.in_(ids))
            )
            old_files = list((await s.execute(old_q)).scalars())
            same = next(
                (f for f in old_files if f.relative_path == rel and f.destination_id == dest.id),
                None,
            )
            mf = same or MediaFile(
                storage_node_id=dest.node_id, destination_id=dest.id, relative_path=rel
            )
            mf.size, mf.mtime, mf.quick_hash = st.st_size, utcnow(), qh
            mf.quality, mf.custom_format_score = _quality_json(parsed, quality), score
            mf.release_group, mf.original_name, mf.link_mode = parsed.group, video.path, mode
            mf.imported_at = utcnow()
            if same is None:
                s.add(mf)
                await s.flush()
                for wid in ids:
                    s.add(MediaFileItem(media_file_id=mf.id, wanted_item_id=wid))
            for old in old_files:
                if old is same:
                    continue
                old_dest = (
                    await s.get(Destination, old.destination_id) if old.destination_id else None
                )
                if old_dest is not None:
                    old_path = Path(old_dest.path) / old.relative_path
                    recycle = str(Path(old_dest.path) / RECYCLE_DIR) if old_dest.recycle else None
                    with contextlib.suppress(StorageError):
                        await self.storage.local.delete(str(old_path), recycle_dir=recycle)
                    replaced.append(old.relative_path)
                await s.delete(old)
            await s.execute(
                update(WantedItem)
                .where(WantedItem.id.in_(ids))
                .values(
                    state="acquired",
                    lock="acquired",
                    cutoff_met=cutoff_met,
                    version=WantedItem.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            for w in waiting:
                s.add(
                    HistoryEvent(
                        wanted_item_id=w.id,
                        title_id=w.title_id,
                        type="upgraded" if replaced else "imported",
                        data={
                            "path": rel,
                            "destination": dest.name,
                            "quality": quality,
                            "link_mode": mode,
                            "download_id": d.id,
                            "release_title": grab.release_title,
                            "replaced": replaced,
                        },
                    )
                )
            await s.commit()
        subject = Subject(type="media_file", id=mf.id)
        await self.bus.emit(
            "import.completed",
            subject=subject,
            name=title.name,
            year=title.year,
            path=rel,
            destination=dest.name,
            quality=quality,
            link_mode=mode,
        )
        if replaced:
            await self.bus.emit(
                "file.upgraded", subject=subject, name=title.name, path=rel, replaced=replaced
            )
        return {
            "media_file_id": mf.id,
            "path": str(dst),
            "link_mode": mode,
            "cutoff_met": cutoff_met,
        }

    async def _patterns(self, dest: Destination) -> dict[str, str]:
        async with self.db.sessionmaker() as s:
            row = (
                await s.get(NamingTemplate, dest.naming_template_id)
                if dest.naming_template_id
                else None
            )
        return {**DEFAULT_PATTERNS, **(row.patterns if row else {})}

    async def _place(self, src: Path, dst: Path, dest: Destination) -> str:
        """Put src at dst using the destination's link mode. Returns the mode used."""
        local = self.storage.local
        existing = await asyncio.to_thread(_compare, src, dst)
        if existing in ("same", "linked"):
            return "existing"  # re-import of the same file
        if existing == "different":
            raise ImportProblem(f"a different file already exists at {dst}", retry=False)
        mode = dest.link_mode
        try:
            if mode in ("hardlink_or_copy", "hardlink_only", "move"):
                # "move": link (or copy) now so seeding keeps working; the torrent's own
                # copy goes when the client's seeding goal removes it with its data.
                try:
                    await local.hardlink(str(src), str(dst))
                    return "hardlink"
                except StorageError as exc:
                    if mode == "hardlink_only":
                        raise ImportProblem(
                            f"hardlink failed ({exc.message}); the destination "
                            "is set to hardlink only",
                            retry=False,
                        ) from None
                await local.copy(str(src), str(dst), verify=True)
                return "copy"
            if mode == "symlink":
                await local.symlink(str(src), str(dst))
                return "symlink"
            await local.copy(str(src), str(dst), verify=True)
            return "copy"
        except StorageError as exc:
            raise ImportProblem(
                f"could not place the file: {exc.message}",
                retry=exc.kind in ("no_space", "io", "verify", "permission"),
            ) from None

    async def retry(self, download_id: str) -> None:
        async with self.db.sessionmaker() as s:
            d = await s.get(Download, download_id)
        if d is None:
            raise NotFoundError("download", download_id)
        if d.status != "completed" or d.import_status in ("imported", "importing"):
            raise ConflictError(
                f"nothing to import (download {d.status}, import {d.import_status})"
            )
        await self._set(download_id, import_status="pending", import_retry_at=None)
        self._wake.set()

    async def imports(self) -> list[Download]:
        async with self.db.sessionmaker() as s:
            q = (
                select(Download)
                .where(Download.import_status.is_not(None))
                .order_by(Download.completed_at.desc())
            )
            return list((await s.execute(q)).scalars())

    # ---- files --------------------------------------------------------------------------------

    async def files(self, title_id: str | None = None) -> list[tuple[MediaFile, list[str]]]:
        async with self.db.sessionmaker() as s:
            q = select(MediaFile).order_by(MediaFile.imported_at.desc())
            if title_id:
                q = (
                    q.join(MediaFileItem, MediaFileItem.media_file_id == MediaFile.id)
                    .join(WantedItem, WantedItem.id == MediaFileItem.wanted_item_id)
                    .where(WantedItem.title_id == title_id)
                    .distinct()
                )
            rows = list((await s.execute(q)).scalars())
            out = []
            for f in rows:
                items = (
                    await s.execute(
                        select(MediaFileItem.wanted_item_id).where(
                            MediaFileItem.media_file_id == f.id
                        )
                    )
                ).scalars()
                out.append((f, list(items)))
            return out

    async def file(self, file_id: str) -> tuple[MediaFile, list[str]]:
        async with self.db.sessionmaker() as s:
            f = await s.get(MediaFile, file_id)
            if f is None:
                raise NotFoundError("file", file_id)
            items = (
                await s.execute(
                    select(MediaFileItem.wanted_item_id).where(MediaFileItem.media_file_id == f.id)
                )
            ).scalars()
            return f, list(items)

    async def delete_file(self, file_id: str, *, delete_from_disk: bool, actor: Actor) -> None:
        """Forget the file (and recycle it when asked). Its items stay locked: deleting a
        file is not a request to download again (that is redownload / unlock)."""
        f, items = await self.file(file_id)
        recycled = None
        if delete_from_disk and f.destination_id:
            dest = await self.storage.destination(f.destination_id)
            recycle = str(Path(dest.path) / RECYCLE_DIR) if dest.recycle else None
            try:
                recycled = await self.storage.local.delete(
                    str(Path(dest.path) / f.relative_path), recycle_dir=recycle
                )
            except StorageError as exc:
                raise ConflictError(f"could not delete the file: {exc.message}") from None
        async with self.db.sessionmaker() as s:
            row = await s.get(MediaFile, file_id)
            if row is not None:
                await s.delete(row)
            for wid in items:
                w = await s.get(WantedItem, wid)
                if w is not None:
                    s.add(
                        HistoryEvent(
                            wanted_item_id=wid,
                            title_id=w.title_id,
                            type="deleted",
                            data={
                                "path": f.relative_path,
                                "recycled": recycled,
                                "from_disk": delete_from_disk,
                            },
                        )
                    )
            await s.commit()
        await self.bus.emit(
            "file.deleted",
            subject=Subject(type="media_file", id=file_id),
            actor=actor,
            path=f.relative_path,
            from_disk=delete_from_disk,
        )

    # ---- naming templates ---------------------------------------------------------------------

    async def templates(self) -> list[NamingTemplate]:
        async with self.db.sessionmaker() as s:
            return list(
                (await s.execute(select(NamingTemplate).order_by(NamingTemplate.name))).scalars()
            )

    async def template(self, template_id: str) -> NamingTemplate:
        async with self.db.sessionmaker() as s:
            row = await s.get(NamingTemplate, template_id)
        if row is None:
            raise NotFoundError("naming template", template_id)
        return row

    @staticmethod
    def check_patterns(patterns: dict[str, str]) -> dict[str, str]:
        try:
            return validate_patterns(patterns)
        except TemplateError as exc:
            raise ProblemError(
                422,
                "Validation Failed",
                slug="validation",
                errors=[FieldError(field="body.patterns", message=str(exc))],
            ) from None

    async def create_template(
        self, name: str, patterns: dict[str, str], *, actor: Actor
    ) -> NamingTemplate:
        row = NamingTemplate(name=name, patterns=self.check_patterns(patterns))
        async with self.db.sessionmaker() as s:
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                raise ConflictError(f"a naming template named {name!r} exists") from None
        await self.bus.emit(
            "library.template_created",
            subject=Subject(type="naming_template", id=row.id),
            actor=actor,
            name=name,
        )
        return row

    async def update_template(
        self, template_id: str, changes: dict[str, Any], *, if_match: str | None, actor: Actor
    ) -> NamingTemplate:
        async with self.db.sessionmaker() as s:
            row = await s.get(NamingTemplate, template_id)
            if row is None:
                raise NotFoundError("naming template", template_id)
            check_if_match(row, if_match)
            if "patterns" in changes:
                row.patterns = self.check_patterns({**row.patterns, **changes["patterns"]})
            if "name" in changes:
                row.name = changes["name"]
            await s.commit()
        await self.bus.emit(
            "library.template_updated",
            subject=Subject(type="naming_template", id=row.id),
            actor=actor,
            name=row.name,
        )
        return row
