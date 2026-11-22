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
import os
import re
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import structlog
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fetchbox.core.concurrency import check_if_match
from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, FieldError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.jobs import JobContext, JobRunner
from fetchbox.modules.downloads.models import Download
from fetchbox.modules.downloads.service import DownloadsService, in_library
from fetchbox.modules.library.models import MediaFile, MediaFileItem, NamingTemplate
from fetchbox.modules.library.naming import (
    DEFAULT_PATTERNS,
    KIND_KEYS,
    OLD_EPISODE_FILE,
    TemplateError,
    episode_path,
    movie_path,
    validate_patterns,
)
from fetchbox.modules.library.probe import MediaInfo, probe
from fetchbox.modules.media.models import Episode, HistoryEvent, Title, WantedItem
from fetchbox.modules.media.service import MediaService
from fetchbox.modules.profiles.service import ProfilesService, to_spec
from fetchbox.modules.releases.models import Grab, GrabItem
from fetchbox.modules.releases.parser import ParsedRelease, parse_release
from fetchbox.modules.storage.models import Destination
from fetchbox.modules.storage.service import RECYCLE_DIR, StorageService
from fetchbox.providers.download_clients import ClientFile, DownloadClientError
from fetchbox.providers.storage import StorageError
from fetchbox.providers.storage.local import inside, quick_hash

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


def videos_in(files: list[ClientFile]) -> list[ClientFile]:
    """Video files, samples left out, in path order."""
    out = [
        f
        for f in files
        if PurePosixPath(f.path.replace("\\", "/")).suffix.lower().lstrip(".") in VIDEO_EXTS
        and not any(_SAMPLE.search(part) for part in PurePosixPath(f.path.replace("\\", "/")).parts)
    ]
    return sorted(out, key=lambda f: f.path)


_SXXEYY = re.compile(r"\b[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})((?:[ ._-]?-?[Ee]\d{1,3})*)")


def release_episodes(
    p: ParsedRelease,
    by_air: dict[date, tuple[int, int]],
    by_abs: dict[int, tuple[int, int]],
) -> list[tuple[int, int]]:
    """(season, episode) pairs a parsed name points at: S/E numbers, an air date (daily
    shows) or absolute numbers (anime)."""
    if p.seasons and p.episodes:
        return [(p.seasons[0], e) for e in p.episodes]
    if p.air_date and p.air_date in by_air:
        return [by_air[p.air_date]]
    return [by_abs[a] for a in p.absolute_episodes if a in by_abs]


def file_episodes(
    path: str,
    by_air: dict[date, tuple[int, int]],
    by_abs: dict[int, tuple[int, int]],
) -> list[tuple[int, int]]:
    """Episodes one file holds, from its own name (then its folder's name)."""
    pure = PurePosixPath(path.replace("\\", "/"))
    found = release_episodes(parse_release(pure.name), by_air, by_abs)
    if found:
        return found
    m = _SXXEYY.search(pure.name)  # a bare "S01E05.mkv"
    if m:
        season, first = int(m.group(1)), int(m.group(2))
        more = [int(x) for x in re.findall(r"\d{1,3}", m.group(3) or "")]
        return [(season, e) for e in [first, *more]]
    return []


def episode_context(
    title: Title,
    parsed: ParsedRelease,
    quality: str,
    info: MediaInfo | None,
    season: int,
    eps: list[Episode],
) -> dict[str, Any]:
    """Naming tokens for an episode file (several episodes: a multi-episode file)."""
    ctx = movie_context(title, parsed, quality, info)
    numbers = sorted(e.number for e in eps)
    first = min(eps, key=lambda e: e.number)
    names = [e.name for e in sorted(eps, key=lambda e: e.number) if e.name]
    ctx.update(
        TvdbId=title.tvdb_id,
        Season=season,
        Episode=numbers[0],
        EpisodeEnd=numbers[-1] if len(numbers) > 1 else None,
        EpisodeTitle=" + ".join(dict.fromkeys(names)) or None,
        AirDate=first.air_date.isoformat() if first.air_date else None,
    )
    return ctx


def movie_context(
    title: Title, parsed: ParsedRelease, quality: str, info: MediaInfo | None = None
) -> dict[str, Any]:
    """Naming tokens. The release name wins; what the file itself says (``info``) fills
    what the name leaves out (resolution, codec, HDR, audio)."""
    tags = [
        t for t in ("PROPER" if parsed.proper else None, "REPACK" if parsed.repack else None) if t
    ]
    codecs = parsed.audio_codecs or (info.audio_codecs if info else [])
    audio = " ".join(
        x
        for x in (
            codecs[0].upper() if codecs else None,
            parsed.audio_channels or (info.audio_channels if info else None),
            "Atmos" if parsed.atmos else None,
        )
        if x
    )
    resolution = parsed.resolution or (info.resolution if info else None)
    codec = parsed.video_codec or (info.video_codec if info else None)
    hdr = parsed.hdr or (info.hdr if info else [])
    return {
        "Title": title.name,
        "Year": title.year,
        "TmdbId": title.tmdb_id,
        "ImdbId": title.imdb_id,
        "Edition": parsed.edition,
        # "Unknown" says nothing: the file's resolution if read, else left out.
        "Quality": quality
        if not quality.startswith("Unknown")
        else (f"{resolution}p" if resolution else None),
        "Resolution": f"{resolution}p" if resolution else None,
        "Source": SOURCE_NAMES.get(parsed.source or "", parsed.source),
        "HDR": hdr,
        "Codec": CODEC_NAMES.get(codec or "", codec),
        "Audio": audio or None,
        "Group": parsed.group,
        "Proper": " ".join(tags) or None,
        "OriginalName": parsed.raw,
    }


def _rename_into(src: Path, dst: Path) -> None:
    """A file to its library place, on the same drive: a rename, never a copy."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def _quality_json(
    parsed: ParsedRelease, quality: str, info: MediaInfo | None = None
) -> dict[str, Any]:
    return {
        "name": quality,
        "source": parsed.source,
        "resolution": parsed.resolution or (info.resolution if info else None),
        "modifier": parsed.modifier,
        "revision": parsed.revision.version,
        "video_codec": parsed.video_codec or (info.video_codec if info else None),
        "hdr": parsed.hdr or (info.hdr if info else []),
        "audio_codecs": parsed.audio_codecs or (info.audio_codecs if info else []),
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
        media: MediaService,
    ) -> None:
        self.db = db
        self.bus = bus
        self.jobs = jobs
        self.storage = storage
        self.downloads = downloads
        self.profiles = profiles
        self.media = media
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
            elif (row.patterns or {}).get("episode_file") == OLD_EPISODE_FILE:
                # The old default printed no brackets around the quality: update it, unless
                # the user changed it.
                row.patterns = {**row.patterns, "episode_file": DEFAULT_PATTERNS["episode_file"]}
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
        except Exception as raw:
            # Never leave a download "importing" (and its items "downloading") forever: an
            # unexpected error is a problem to show and retry like any other.
            if isinstance(raw, ImportProblem):
                exc = raw
            else:
                log.exception("library.import_crashed", download_id=d.id)
                detail = f"import failed unexpectedly: {type(raw).__name__}: {raw}"
                exc = ImportProblem(detail[:500], retry=True)
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
        client = self.downloads.clients.get(d.client_id or "")
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
        dest = await self._destination(title)
        release = grab.release
        parsed = (
            ParsedRelease.model_validate(release["parsed"])
            if release.get("parsed")
            else parse_release(grab.release_title)
        )
        quality = release.get("quality") or parsed.quality
        score = int(release.get("format_score") or 0)
        if title.kind == "series":
            return await self._import_episodes(
                d, grab, title, waiting, dest, parsed, quality, score
            )

        # movie: the biggest video file (samples ignored)
        video = pick_video(await self._files(d))
        if video is None:
            raise ImportProblem("no video file in the download (samples are ignored)", retry=False)
        src = await self._on_disk(d, video)
        patterns = await self._patterns(dest)
        info = await asyncio.to_thread(probe, src)
        rel = movie_path(patterns, movie_context(title, parsed, quality, info), src.suffix)
        dst = Path(dest.path) / rel
        mode = await self._place(src, dst, dest, d, video.path)
        mf, replaced, cutoff_met = await self._record(
            d, grab, title, dest, waiting, rel, dst, mode, parsed, quality, score, info, video.path
        )
        await self._announce(title, dest, mf, rel, quality, mode, replaced)
        return {
            "media_file_id": mf.id,
            "path": str(dst),
            "link_mode": mode,
            "cutoff_met": cutoff_met,
        }

    async def _destination(self, title: Title) -> Destination:
        dest = await (
            self.storage.destination(title.destination_id)
            if title.destination_id
            else self.storage.default_destination(title.kind)
        )
        dest = await self.storage.check(dest.id)
        if dest.status not in ("online", "degraded"):
            raise ImportProblem(f"destination {dest.name} is {dest.status}: {dest.status_message}")
        return dest

    async def _on_disk(self, d: Download, video: ClientFile) -> Path:
        """The file's path, after checking it is there and complete."""
        if not d.save_path:
            raise ImportProblem("the download client hasn't said where it saved the files yet")
        src = Path(d.save_path) / video.path
        on_disk = await asyncio.to_thread(_size, src)
        if on_disk is None:
            folder_seen = await asyncio.to_thread(Path(d.save_path).is_dir)
            if not folder_seen:  # typically a client on another machine or in Docker
                raise ImportProblem(
                    f"Fetchbox can't see {d.save_path}, where the client saved this. If the "
                    "client runs on another machine or in Docker, share that folder with this "
                    "computer and add a path mapping (Settings > Download clients > Edit)"
                )
            raise ImportProblem(f"{video.path} is missing from {d.save_path}")
        if on_disk != video.size:
            raise ImportProblem(
                f"{video.path} is incomplete on disk ({on_disk} of {video.size} bytes)"
            )
        return src

    async def _record(
        self,
        d: Download,
        grab: Grab,
        title: Title,
        dest: Destination,
        items: list[WantedItem],
        rel: str,
        dst: Path,
        mode: str,
        parsed: ParsedRelease,
        quality: str,
        score: int,
        info: MediaInfo | None,
        original: str,
    ) -> tuple[MediaFile, list[str], bool]:
        """The media_file for ``dst`` and its items: acquired, locked, cutoff computed; a
        previous file of these items is replaced (to the recycle folder). Returns (file,
        replaced paths, cutoff met)."""
        qh = await asyncio.to_thread(quick_hash, dst)
        st = await asyncio.to_thread(dst.stat)
        profile = to_spec(await self.profiles.get_profile(title.profile_id))
        tier = profile.tier_index(quality)
        cutoff_met = (
            tier is not None
            and tier >= profile.cutoff_index
            and (profile.cutoff_format_score is None or score >= profile.cutoff_format_score)
        )
        replaced: list[str] = []
        # The files this one replaces: stop any torrent still seeding them first, before
        # this transaction (stopping one writes too, and would wait on it)
        async with self.db.sessionmaker() as s:
            prior = (
                await s.execute(
                    select(MediaFile.relative_path, Destination.path)
                    .join(MediaFileItem, MediaFileItem.media_file_id == MediaFile.id)
                    .join(Destination, Destination.id == MediaFile.destination_id)
                    .where(MediaFileItem.wanted_item_id.in_([w.id for w in items]))
                )
            ).all()
        await self.downloads.release_files(
            {str(Path(root) / r) for r, root in prior} - {str(Path(dest.path) / rel)}
        )
        async with self.db.sessionmaker() as s:
            ids = [w.id for w in items]
            old_q = (
                select(MediaFile)
                .join(MediaFileItem, MediaFileItem.media_file_id == MediaFile.id)
                .where(MediaFileItem.wanted_item_id.in_(ids))
            )
            old_files = list((await s.execute(old_q)).scalars().unique())
            same = next(
                (f for f in old_files if f.relative_path == rel and f.destination_id == dest.id),
                None,
            )
            at_path = None
            if same is None:
                # A record already at this path (e.g. left by a title that was removed and
                # added again): the file there is now this one, so take the record over.
                at_path = (
                    await s.execute(
                        select(MediaFile).where(
                            MediaFile.storage_node_id == dest.node_id,
                            MediaFile.relative_path == rel,
                        )
                    )
                ).scalar_one_or_none()
                if at_path is not None:
                    await s.execute(
                        delete(MediaFileItem).where(MediaFileItem.media_file_id == at_path.id)
                    )
                    at_path.destination_id = dest.id
            mf = (
                same
                or at_path
                or MediaFile(
                    storage_node_id=dest.node_id, destination_id=dest.id, relative_path=rel
                )
            )
            mf.size, mf.mtime, mf.quick_hash = st.st_size, utcnow(), qh
            mf.quality, mf.custom_format_score = _quality_json(parsed, quality, info), score
            mf.mediainfo = info.to_json() if info else None
            mf.release_group, mf.original_name, mf.link_mode = parsed.group, original, mode
            mf.imported_at = utcnow()
            if same is None:
                s.add(mf)
                await s.flush()
                for wid in ids:
                    s.add(MediaFileItem(media_file_id=mf.id, wanted_item_id=wid))
            for old in old_files:
                if old is same:
                    continue
                serves = set(
                    (
                        await s.execute(
                            select(MediaFileItem.wanted_item_id).where(
                                MediaFileItem.media_file_id == old.id
                            )
                        )
                    ).scalars()
                )
                if not serves <= set(ids):
                    # A multi-episode file also serving other episodes stays for them; only
                    # the episodes the new file covers move over.
                    await s.execute(
                        delete(MediaFileItem).where(
                            MediaFileItem.media_file_id == old.id,
                            MediaFileItem.wanted_item_id.in_(ids),
                        )
                    )
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
            for w in items:
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
        return mf, replaced, cutoff_met

    async def _announce(
        self,
        title: Title,
        dest: Destination,
        mf: MediaFile,
        rel: str,
        quality: str,
        mode: str,
        replaced: list[str],
    ) -> None:
        subject = Subject(type="media_file", id=mf.id)
        await self.bus.emit(
            "import.completed",
            subject=subject,
            name=title.name,
            year=title.year,
            path=rel,
            destination=dest.name,
            destination_id=dest.id,
            folders=[str((Path(dest.path) / rel).parent)],  # for media servers to rescan
            quality=quality,
            link_mode=mode,
        )
        if replaced:
            await self.bus.emit(
                "file.upgraded", subject=subject, name=title.name, path=rel, replaced=replaced
            )

    async def _import_episodes(
        self,
        d: Download,
        grab: Grab,
        title: Title,
        waiting: list[WantedItem],
        dest: Destination,
        parsed: ParsedRelease,
        quality: str,
        score: int,
    ) -> dict[str, Any]:
        """Every video file is matched to the episodes it holds (by its own name, or the
        release's for a single-episode download); a file holding several episodes (multi-
        episode) serves them all. Wanted episodes the download doesn't have go back to
        missing, to be searched again."""
        rows = await self.media.episode_items(title.id, item_ids=[w.id for w in waiting])
        by_key = {(n, ep.number): (ep, w) for n, ep, w in rows}
        by_air = {ep.air_date: (n, ep.number) for n, ep, _ in rows if ep.air_date}
        by_abs = {ep.absolute_number: (n, ep.number) for n, ep, _ in rows if ep.absolute_number}
        videos = videos_in(await self._files(d))
        if not videos:
            raise ImportProblem("no video file in the download (samples are ignored)", retry=False)
        single = len(videos) == 1
        patterns = await self._patterns(dest)
        imported: list[dict[str, Any]] = []
        done: set[str] = set()
        for video in videos:
            keys = file_episodes(video.path, by_air, by_abs)
            if not keys and single:
                keys = release_episodes(parsed, by_air, by_abs)
            matched = [k for k in keys if k in by_key and by_key[k][1].id not in done]
            if not matched:
                continue  # an episode nobody asked for, or extras
            src = await self._on_disk(d, video)
            file_parsed = parse_release(PurePosixPath(video.path.replace("\\", "/")).name)
            file_quality = file_parsed.quality if file_parsed.resolution else quality
            info = await asyncio.to_thread(probe, src)
            eps = [by_key[k][0] for k in matched]
            ctx = episode_context(title, file_parsed if file_parsed.resolution else parsed,
                                  file_quality, info, matched[0][0], eps)  # fmt: skip
            rel = episode_path(patterns, ctx, src.suffix)
            dst = Path(dest.path) / rel
            mode = await self._place(src, dst, dest, d, video.path)
            items = [by_key[k][1] for k in matched]
            mf, replaced, _ = await self._record(
                d, grab, title, dest, items, rel, dst, mode,
                file_parsed if file_parsed.resolution else parsed, file_quality, score, info,
                video.path,
            )  # fmt: skip
            await self._announce(title, dest, mf, rel, file_quality, mode, replaced)
            done.update(w.id for w in items)
            imported.append({"media_file_id": mf.id, "path": str(dst), "episodes": matched})
        if not imported:
            raise ImportProblem(
                "no file in the download is one of the wanted episodes", retry=False
            )
        missing = [w for w in waiting if w.id not in done]
        if missing:  # not in this download after all: search for them again
            await self._not_in_download(d, grab, missing)
        return {"files": imported, "not_in_download": [w.id for w in missing]}

    async def _not_in_download(self, d: Download, grab: Grab, items: list[WantedItem]) -> None:
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(WantedItem)
                .where(WantedItem.id.in_([w.id for w in items]))
                .values(state="missing", next_search_at=None, version=WantedItem.version + 1)
                .execution_options(synchronize_session=False)
            )
            for w in items:
                s.add(
                    HistoryEvent(
                        wanted_item_id=w.id,
                        title_id=w.title_id,
                        type="failed",
                        data={
                            "download_id": d.id,
                            "release_title": grab.release_title,
                            "reason": "not in the download",
                        },
                    )
                )
            await s.commit()

    async def _patterns(self, dest: Destination) -> dict[str, str]:
        async with self.db.sessionmaker() as s:
            row = (
                await s.get(NamingTemplate, dest.naming_template_id)
                if dest.naming_template_id
                else None
            )
        return {**DEFAULT_PATTERNS, **(row.patterns if row else {})}

    async def _place(
        self,
        src: Path,
        dst: Path,
        dest: Destination,
        d: Download | None = None,
        rel: str | None = None,
    ) -> str:
        """Put src at dst. Returns how: "in_place" (the built-in engine moved its file
        into the library and seeds it from there), "move" (a direct download, renamed into
        place), or, for external clients, the destination's link mode."""
        try:
            inside(dest.path, dst)  # never write outside the library folder
        except StorageError as exc:
            raise ImportProblem(exc.message, retry=False) from None
        local = self.storage.local
        existing = await asyncio.to_thread(_compare, src, dst)
        if existing in ("same", "linked"):
            return "existing"  # re-import of the same file
        if existing == "different":
            raise ImportProblem(f"a different file already exists at {dst}", retry=False)
        if d is not None and in_library(d):
            client = self.downloads.clients.get(d.client_id or "")
            try:
                if client is not None and client.can_relocate and rel is not None:
                    await client.relocate(d.client_ref, {rel: str(dst)})
                    return "in_place"
                await asyncio.to_thread(_rename_into, src, dst)
                return "move"
            except (DownloadClientError, OSError) as exc:
                raise ImportProblem(
                    f"could not move the file into place: {getattr(exc, 'message', exc)}"
                ) from None
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
            await self.downloads.release_files({str(Path(dest.path) / f.relative_path)})
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

    # ---- library settings: naming per kind, renaming existing files --------------------------

    async def naming(self, kind: str) -> tuple[Destination, dict[str, str]]:
        """The default library folder for movies or series, and its naming patterns."""
        dest = await self.storage.default_destination(kind)
        return dest, await self._patterns(dest)

    async def set_naming(
        self, kind: str, patterns: dict[str, str], *, actor: Actor
    ) -> tuple[Destination, dict[str, str]]:
        """Save naming for a kind's library folder. A template shared with another folder
        is copied first, so changing movie naming never renames TV (or the reverse)."""
        dest = await self.storage.default_destination(kind)
        keys = (*KIND_KEYS[kind], *(("season_folder",) if kind == "series" else ()))
        changes = {k: v for k, v in patterns.items() if k in keys}
        merged = self.check_patterns({**await self._patterns(dest), **changes})
        async with self.db.sessionmaker() as s:
            row = await s.get(Destination, dest.id)
            assert row is not None
            tpl = (
                await s.get(NamingTemplate, row.naming_template_id)
                if row.naming_template_id
                else None
            )
            shared = tpl is not None and bool(
                await s.scalar(
                    select(Destination.id).where(
                        Destination.naming_template_id == tpl.id, Destination.id != row.id
                    )
                )
            )
            if tpl is None or shared or tpl.name == DEFAULT_TEMPLATE:
                tpl = NamingTemplate(name=f"{row.name} naming ({row.id[-6:]})", patterns=merged)
                s.add(tpl)
                await s.flush()
                row.naming_template_id = tpl.id  # the version moves on by itself
            else:
                tpl.patterns = merged
            await s.commit()
        await self.bus.emit(
            "library.naming_updated",
            subject=Subject(type="destination", id=dest.id),
            actor=actor,
            kind=kind,
        )
        return await self.naming(kind)

    async def rename_plan(self, kind: str) -> list[dict[str, Any]]:
        """Files of this kind whose name differs from what the naming would give now:
        [{file_id, title_id, title, destination_id, root, current, new}]."""
        async with self.db.sessionmaker() as s:
            rows = (
                await s.execute(
                    select(MediaFile, Destination, WantedItem)
                    .join(Destination, Destination.id == MediaFile.destination_id)
                    .join(MediaFileItem, MediaFileItem.media_file_id == MediaFile.id)
                    .join(WantedItem, WantedItem.id == MediaFileItem.wanted_item_id)
                    .where(Destination.media_kind == kind)
                    .order_by(MediaFile.relative_path)
                )
            ).all()
        files: dict[str, tuple[MediaFile, Destination, list[WantedItem]]] = {}
        for mf, dest, w in rows:
            files.setdefault(mf.id, (mf, dest, []))[2].append(w)
        patterns: dict[str, dict[str, str]] = {}
        titles: dict[str, Title] = {}
        plan: list[dict[str, Any]] = []
        for mf, dest, items in files.values():
            if dest.id not in patterns:
                patterns[dest.id] = await self._patterns(dest)
            tid = items[0].title_id
            if tid not in titles:
                titles[tid] = await self.media.get_title(tid)
            title = titles[tid]
            new = await self._name_for(mf, title, items, patterns[dest.id])
            if new is None or new == mf.relative_path:
                continue
            plan.append(
                {
                    "file_id": mf.id,
                    "title_id": title.id,
                    "title": title.name,
                    "destination_id": dest.id,
                    "root": dest.path,
                    "current": mf.relative_path,
                    "new": new,
                }
            )
        return plan

    async def _name_for(
        self, mf: MediaFile, title: Title, items: list[WantedItem], patterns: dict[str, str]
    ) -> str | None:
        """Where the naming puts an existing file, from what was recorded at import."""
        q = mf.quality or {}
        parsed = parse_release(PurePosixPath((mf.original_name or "").replace("\\", "/")).name)
        if mf.release_group and not parsed.group:
            parsed.group = mf.release_group
        info = _info(mf.mediainfo)
        quality = str(q.get("name") or parsed.quality)
        ext = PurePosixPath(mf.relative_path).suffix
        if title.kind == "movie":
            return movie_path(patterns, movie_context(title, parsed, quality, info), ext)
        rows = await self.media.episode_items(title.id, item_ids=[w.id for w in items])
        if not rows:
            return None
        season = rows[0][0]
        ctx = episode_context(title, parsed, quality, info, season, [ep for _, ep, _ in rows])
        return episode_path(patterns, ctx, ext)

    async def rename(
        self, kind: str, file_ids: list[str] | None, *, actor: Actor
    ) -> dict[str, Any]:
        """Move files to the names the naming gives now (all that differ, or the listed
        ones). Files stay on the same disk, so this is a rename, not a copy; folders left
        empty are removed. Returns {renamed: [...], failed: [{file_id, current, error}]}."""
        plan = await self.rename_plan(kind)
        if file_ids is not None:
            plan = [p for p in plan if p["file_id"] in set(file_ids)]
        renamed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for p in plan:
            root = Path(p["root"])
            try:
                src, dst = inside(root, p["current"]), inside(root, p["new"])
            except StorageError as exc:
                failed.append({"file_id": p["file_id"], "current": p["current"],
                               "error": exc.message})  # fmt: skip
                continue
            try:
                # a file the torrent still seeds from moves through the engine
                if not await self.downloads.move_seeding_file(str(src), str(dst)):
                    await asyncio.to_thread(_move, src, dst, root)
                else:
                    await asyncio.to_thread(_prune, src.parent, root)
            except (OSError, DownloadClientError) as exc:
                msg = getattr(exc, "message", None) or str(exc)
                failed.append({"file_id": p["file_id"], "current": p["current"], "error": msg})
                continue
            async with self.db.sessionmaker() as s:
                mf = await s.get(MediaFile, p["file_id"])
                if mf is not None:
                    mf.relative_path = p["new"]
                    await s.commit()
            renamed.append(p)
        if renamed:
            await self.bus.emit(
                "library.renamed",
                subject=Subject(type="destination", id=renamed[0]["destination_id"]),
                actor=actor,
                kind=kind,
                count=len(renamed),
                # old and new folders: the server drops the old entry, finds the new one
                folders=sorted(
                    {
                        str((Path(p["root"]) / p[k]).parent)
                        for p in renamed
                        for k in ("current", "new")
                    }
                ),
            )
        return {"renamed": renamed, "failed": failed}

    async def adopt(
        self, title: Title, dest: Destination, rel: str, items: list[WantedItem]
    ) -> bool:
        """Record a file already in the library folder (``rel``, relative to it) for
        ``items``: they become acquired, cutoff judged by the file's quality. Items that
        already have a file, or a file already recorded, are left alone. Returns whether
        anything was recorded."""
        try:
            path = inside(dest.path, rel)  # a path from the API: stay in the folder
        except StorageError:
            return False
        if not items or not await asyncio.to_thread(path.is_file):
            return False
        parsed = parse_release(PurePosixPath(rel).name)
        info = await asyncio.to_thread(probe, path)
        quality = parsed.quality
        if quality.startswith("Unknown") and info and info.resolution:
            quality = f"Unknown-{info.resolution}p"  # the profile may not allow it: an upgrade
        profile = to_spec(await self.profiles.get_profile(title.profile_id))
        tier = profile.tier_index(quality)
        cutoff_met = tier is not None and tier >= profile.cutoff_index
        st = await asyncio.to_thread(path.stat)
        async with self.db.sessionmaker() as s:
            taken = set(
                (
                    await s.execute(
                        select(MediaFileItem.wanted_item_id).where(
                            MediaFileItem.wanted_item_id.in_([w.id for w in items])
                        )
                    )
                ).scalars()
            )
            free = [w for w in items if w.id not in taken]
            existing = (
                await s.execute(
                    select(MediaFile).where(
                        MediaFile.destination_id == dest.id, MediaFile.relative_path == rel
                    )
                )
            ).scalar_one_or_none()
            if not free:
                return False
            mf = existing or MediaFile(
                storage_node_id=dest.node_id,
                destination_id=dest.id,
                relative_path=rel,
                size=st.st_size,
                mtime=utcnow(),
                quick_hash=None,
                quality=_quality_json(parsed, quality, info),
                custom_format_score=0,
                mediainfo=info.to_json() if info else None,
                release_group=parsed.group,
                original_name=PurePosixPath(rel).name,
                link_mode="existing",
                imported_at=utcnow(),
            )
            if existing is None:
                s.add(mf)
                await s.flush()
            s.add_all([MediaFileItem(media_file_id=mf.id, wanted_item_id=w.id) for w in free])
            await s.execute(
                update(WantedItem)
                .where(WantedItem.id.in_([w.id for w in free]))
                .values(
                    state="acquired",
                    lock="acquired",
                    cutoff_met=cutoff_met,
                    version=WantedItem.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            s.add_all(
                [
                    HistoryEvent(
                        wanted_item_id=w.id,
                        title_id=title.id,
                        type="imported",
                        data={
                            "path": rel,
                            "destination": dest.name,
                            "quality": quality,
                            "link_mode": "existing",
                            "existing": True,
                        },
                    )
                    for w in free
                ]
            )
            await s.commit()
        return True


def _info(data: dict[str, Any] | None) -> MediaInfo | None:
    if not data:
        return None
    fields = set(MediaInfo.__dataclass_fields__)
    return MediaInfo(**{k: v for k, v in data.items() if k in fields})


def _prune(folder: Path, root: Path) -> None:
    """Remove ``folder`` and its parents while empty, up to ``root``."""
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()  # only if empty
        except OSError:
            break
        folder = folder.parent


def _move(src: Path, dst: Path, root: Path) -> None:
    """Rename ``src`` to ``dst`` (same disk), never over another file; then remove
    the folders ``src`` leaves empty, up to ``root``."""
    if not src.exists():
        raise FileNotFoundError(f"{src} is not there any more")
    if dst.exists() and not dst.samefile(src):
        raise FileExistsError(f"{dst} already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    parent = src.parent
    while parent != root and root in parent.parents:
        try:
            parent.rmdir()  # only if empty
        except OSError:
            break
        parent = parent.parent
