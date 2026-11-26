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
import errno
import os
import re
import shutil
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import structlog
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fetchbox.core import kinds, macfiles
from fetchbox.core.concurrency import check_if_match
from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, FieldError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.jobs import JobContext, JobRunner
from fetchbox.modules.downloads.models import Download
from fetchbox.modules.downloads.service import DownloadsService, in_library
from fetchbox.modules.library.models import (
    MediaFile,
    MediaFileItem,
    MediaFileSidecar,
    NamingTemplate,
)
from fetchbox.modules.library.naming import (
    DEFAULT_PATTERNS,
    KIND_KEYS,
    OLD_EPISODE_FILE,
    TemplateError,
    episode_path,
    existing_season_folder,
    movie_path,
    render,
    safe_component,
    series_folder,
    validate_patterns,
)
from fetchbox.modules.library.probe import MediaInfo, probe
from fetchbox.modules.library.sidecars import check_suffix, sidecar_rel, write_atomically
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
VIDEO_EXTS = kinds.VIDEO_EXTS


def is_video(path: str) -> bool:
    """A video file by its name (a macOS ._ file beside one is not)."""
    p = PurePosixPath(path.replace("\\", "/"))
    return p.suffix.lower().lstrip(".") in VIDEO_EXTS and not macfiles.is_appledouble(p.name)


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
            if p.is_file() and not macfiles.is_appledouble(p.name)
        ]
    return []


class ImportProblem(Exception):
    """An import that can't proceed now; ``retry`` False means it never will."""

    def __init__(self, message: str, *, retry: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.retry = retry


def _season_on_disk(show: Path, season: int) -> str | None:
    """The folder a show already on disk keeps ``season`` in ("" for loose episodes),
    or None (nothing there yet: the naming decides)."""
    try:
        entries = list(show.iterdir())
    except OSError:
        return None
    folders = [e.name for e in entries if e.is_dir() and not e.name.startswith(".")]
    loose = any(e.is_file() and is_video(e.name) for e in entries)
    return existing_season_folder(folders, loose, season)


def pick_video(files: list[ClientFile], *, kind: str = "movie") -> ClientFile | None:
    videos = [
        f
        for f in files
        if is_video(f.path)
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
        if is_video(f.path)
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


async def _cover(meta: dict[str, Any]) -> bytes | None:
    """The album's front cover (Cover Art Archive), for embedding; None if unavailable."""
    url = meta.get("cover")
    if not url:
        return None
    import httpx

    url = str(url).replace("front-250", "front-500")
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
            r = await http.get(url, headers={"User-Agent": "Fetchbox"})
        if r.status_code == 200 and r.content[:3] == b"\xff\xd8\xff":  # a JPEG
            return r.content
    except httpx.HTTPError:
        pass
    return None


def _tidy_placed(src: Path, dst: Path) -> None:
    macfiles.tidy(dst)
    if not src.exists():  # moved: its ._ file stayed behind
        with contextlib.suppress(OSError):
            (src.parent / f"{macfiles.PREFIX}{src.name}").unlink(missing_ok=True)


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
        # Settings > Library folders > Music (set by the app from the config)
        self.music_naming: Any = lambda: None
        # the MusicService (set by the app): fetches an album's track list at import
        self.music: Any = None
        # Plugins' media kinds import through the plugin (set by the app when it has them):
        # (download, grab, title, waiting items, destination) -> the import's result
        self.plugin_import: Any = None

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
        if title.kind == "music":
            return await self._import_album(d, grab, title, waiting, dest)
        if title.kind == "series":
            return await self._import_episodes(
                d, grab, title, waiting, dest, parsed, quality, score
            )
        if title.kind != "movie":  # a plugin's media kind (comics): its plugin says which file
            if self.plugin_import is None:
                raise ImportProblem(f"no plugin for {title.kind} is running")
            result: dict[str, Any] = await self.plugin_import(d, grab, title, waiting, dest)
            return result

        # movie: the biggest video file (samples ignored)
        video = pick_video(await self._files(d))
        if video is None:
            raise ImportProblem("no video file in the download (samples are ignored)", retry=False)
        src = await self._on_disk(d, video)
        patterns = await self._patterns(dest)
        info = await asyncio.to_thread(probe, src)
        rel = movie_path(
            patterns, movie_context(title, parsed, quality, info), src.suffix, folder=title.folder
        )
        dst = Path(dest.path) / rel
        mode = await self._place(src, dst, dest, d, video.path)
        mf, replaced, cutoff_met = await self._record(
            d, grab, title, dest, waiting, rel, dst, mode, parsed, quality, score, info, video.path
        )
        await self.remember_folder(title, rel.split("/")[0])
        await self._announce(title, dest, mf, rel, quality, mode, replaced)
        return {
            "media_file_id": mf.id,
            "path": str(dst),
            "link_mode": mode,
            "cutoff_met": cutoff_met,
        }

    async def _fetch_tracks(self, album_id: str, files: int) -> list[Any]:
        from fetchbox.modules.media.models import Track
        from fetchbox.providers.metadata.musicbrainz import MusicBrainzError

        if self.music is not None:
            try:
                eds = await self.music.editions(album_id)
                fit = next((e for e in eds if e.track_count == files), None)
                await self.music.choose_edition(album_id, fit.id if fit else None)
            except MusicBrainzError as exc:
                raise ImportProblem(
                    f"the album's track list isn't known yet (MusicBrainz: {exc.message})"
                ) from None
        async with self.db.sessionmaker() as s:
            rows = list(
                (
                    await s.execute(
                        select(Track)
                        .where(Track.album_id == album_id)
                        .order_by(Track.disc, Track.number)
                    )
                ).scalars()
            )
        if not rows:
            raise ImportProblem("MusicBrainz has no track list for this album yet")
        return rows

    async def _import_album(
        self, d: Download, grab: Grab, title: Title, waiting: list[WantedItem], dest: Destination
    ) -> dict[str, Any]:
        """Every track of the wanted edition gets its file, or nothing is imported."""
        from fetchbox.modules.library import music_files as mfiles
        from fetchbox.modules.media.models import Album, Track

        w = waiting[0]
        async with self.db.sessionmaker() as s:
            album = await s.get(Album, w.album_id) if w.album_id else None
            if album is None:
                raise ImportProblem("the album was removed", retry=False)
            rows = list(
                (
                    await s.execute(
                        select(Track)
                        .where(Track.album_id == album.id)
                        .order_by(Track.disc, Track.number)
                    )
                ).scalars()
            )
            old = list(
                (
                    await s.execute(
                        select(MediaFile)
                        .join(MediaFileItem, MediaFileItem.media_file_id == MediaFile.id)
                        .where(MediaFileItem.wanted_item_id == w.id)
                    )
                ).scalars()
            )
        audio_files = [f for f in await self._files(d) if mfiles.is_audio(f.path)]
        if not audio_files:
            raise ImportProblem("no audio files in the download", retry=False)
        if not rows:
            # No edition picked yet (an album not monitored is skipped by the background
            # pick, but can still be downloaded by hand): pick it now, the edition with as
            # many tracks as the download has files (else the standard one).
            rows = await self._fetch_tracks(album.id, len(audio_files))
        audio = []
        for f in audio_files:
            src = await self._on_disk(d, f)
            audio.append(await asyncio.to_thread(mfiles.read, src, f.path))
        tracks = [mfiles.WantedTrack(t.id, t.disc, t.number, t.name, t.length_ms) for t in rows]
        m = mfiles.match(audio, tracks)
        if m.missing:
            names = ", ".join(f"{t.number}. {t.name}" for t in m.missing[:5])
            more = f" and {len(m.missing) - 5} more" if len(m.missing) > 5 else ""
            raise ImportProblem(
                f"{len(m.missing)} of {len(tracks)} tracks have no file ({names}{more}). "
                "Another edition of the album? Choose the edition this download is, or "
                "download again",
                retry=False,
            )
        quality = mfiles.album_quality(list(m.pairs.values()))
        discs = max(t.disc for t in tracks)
        year = album.release_date.year if album.release_date else None
        old_paths = {
            str(Path(dest.path) / of.relative_path): of
            for of in old
            if of.destination_id == dest.id
        }
        recycle = str(Path(dest.path) / RECYCLE_DIR) if dest.recycle else None
        old_sidecars = await self._sidecar_suffixes([of.id for of in old])
        for of in old:  # the old tracks' sidecars go with them (the new files are new)
            if of.destination_id == dest.id and (sc := old_sidecars.get(of.id)):
                await self._drop_sidecar_files(dest, of.relative_path, sc)
        naming = self.music_naming() or mfiles.Naming()
        placed: list[tuple[mfiles.WantedTrack, str, Path, str, mfiles.AudioFile]] = []
        for t in tracks:
            af = m.pairs[t.id]
            rel = mfiles.track_path(
                title.name, album.name, year, t, discs, af.path.suffix, naming,
                artist_folder=title.folder,
            )  # fmt: skip
            dst = Path(dest.path) / rel
            if str(dst) in old_paths and await asyncio.to_thread(dst.exists):
                # an upgrade at the same path (FLAC over FLAC): the old file is recycled,
                # after stopping the torrent that seeds it (if one still does)
                await self.downloads.release_files({str(dst)})
                with contextlib.suppress(StorageError):
                    await self.storage.local.delete(str(dst), recycle_dir=recycle)
            placed.append((t, rel, dst, await self._place(af.path, dst, dest, d, af.rel), af))

        if placed and naming.folder != "artist - album (year)":  # Artist/Album: the artist's
            await self.remember_folder(title, placed[0][1].split("/")[0])
        # Tags change the file's bytes: never in a file the torrent still seeds from
        # (a hardlink or the download itself), only in the library's own copy.
        cover = await _cover(album.meta or {})
        tagged = 0
        for t, _rel, dst, mode, _af in placed:
            if mode not in ("copy", "move"):
                continue
            values = mfiles.TagValues(
                artist=title.name,
                album=album.name,
                year=year,
                title=t.name,
                track=t.number,
                tracks=sum(1 for x in tracks if x.disc == t.disc),
                disc=t.disc,
                discs=discs,
                artist_mbid=title.mbid,
                album_mbid=album.mbid,
                release_mbid=album.edition_mbid,
                recording_mbid=next((r.recording_mbid for r in rows if r.id == t.id), None),
            )
            try:
                await asyncio.to_thread(mfiles.write_tags, dst, values, cover)
                tagged += 1
            except Exception as exc:  # a tag problem never fails an import
                log.warning("music.tags_failed", path=str(dst), error=repr(exc))

        profile = to_spec(await self.profiles.get_profile(title.profile_id))
        tier = profile.tier_index(quality)
        cutoff_met = tier is not None and tier >= profile.cutoff_index
        new_paths = {str(dst) for _, _, dst, _, _ in placed}
        replaced: list[str] = []
        # old tracks about to go: stop any torrent still seeding them first (before this
        # import's own database transaction, which would otherwise wait on it)
        await self.downloads.release_files(
            {str(Path(dest.path) / of.relative_path) for of in old} - new_paths
        )
        async with self.db.sessionmaker() as s:
            for of in old:
                full = str(Path(dest.path) / of.relative_path)
                if full not in new_paths:  # an old track the new files don't replace in place
                    with contextlib.suppress(StorageError):
                        await self.storage.local.delete(full, recycle_dir=recycle)
                    replaced.append(of.relative_path)
                await s.delete(await s.merge(of))
            await s.flush()
            first: MediaFile | None = None
            album_files: list[str] = []
            for t, rel, dst, mode, af in placed:
                st = await asyncio.to_thread(dst.stat)
                row = MediaFile(
                    storage_node_id=dest.node_id,
                    destination_id=dest.id,
                    relative_path=rel,
                    size=st.st_size,
                    mtime=utcnow(),
                    quality={"name": af.quality, "codec": af.codec},
                    original_name=af.rel,
                    link_mode=mode,
                    track_id=t.id,
                    mediainfo={"length_s": af.length_s, "tags_written": mode in ("copy", "move")},
                )
                s.add(row)
                await s.flush()
                s.add(MediaFileItem(media_file_id=row.id, wanted_item_id=w.id))
                first = first or row
                album_files.append(row.id)
            await s.execute(
                update(WantedItem)
                .where(WantedItem.id == w.id)
                .values(
                    state="acquired",
                    lock="acquired",
                    cutoff_met=cutoff_met,
                    version=WantedItem.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            folder = str(PurePosixPath(placed[0][1]).parent)
            mode = placed[0][3]
            s.add(
                HistoryEvent(
                    wanted_item_id=w.id,
                    title_id=w.title_id,
                    type="upgraded" if old else "imported",
                    data={
                        "path": folder,
                        "destination": dest.name,
                        "quality": quality,
                        "link_mode": mode,
                        "download_id": d.id,
                        "release_title": grab.release_title,
                        "replaced": replaced,
                        "tracks": len(placed),
                        "tagged": tagged,
                    },
                )
            )
            await s.commit()
        assert first is not None
        await self._announce(
            title, dest, first, placed[0][1], quality, mode, replaced, file_ids=album_files
        )
        return {
            "album": album.name,
            "tracks": len(placed),
            "quality": quality,
            "link_mode": mode,
            "tagged": tagged,
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
                    if sc := (await self._sidecar_suffixes([old.id])).get(old.id):
                        await self._drop_sidecar_files(old_dest, old.relative_path, sc)
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
        *,
        file_ids: list[str] | None = None,
    ) -> None:
        subject = Subject(type="media_file", id=mf.id)
        await self.bus.emit(
            "import.completed",
            subject=subject,
            name=title.name,
            year=title.year,
            title_id=title.id,
            kind=title.kind,
            file_ids=file_ids or [mf.id],  # an album: every track placed
            path=rel,
            destination=dest.name,
            destination_id=dest.id,
            folders=[str((Path(dest.path) / rel).parent)],  # for media servers to rescan
            quality=quality,
            link_mode=mode,
        )
        if replaced:
            await self.bus.emit(
                "file.upgraded",
                subject=subject,
                name=title.name,
                path=rel,
                replaced=replaced,
                title_id=title.id,
                kind=title.kind,
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
            ctx = episode_context(
                title,
                file_parsed if file_parsed.resolution else parsed,
                file_quality,
                info,
                matched[0][0],
                eps,
            )
            show = title.folder or series_folder(patterns, ctx)
            season = await asyncio.to_thread(_season_on_disk, Path(dest.path) / show, matched[0][0])
            rel = episode_path(patterns, ctx, src.suffix, folder=show, season_folder=season)
            dst = Path(dest.path) / rel
            mode = await self._place(src, dst, dest, d, video.path)
            items = [by_key[k][1] for k in matched]
            mf, replaced, _ = await self._record(
                d,
                grab,
                title,
                dest,
                items,
                rel,
                dst,
                mode,
                file_parsed if file_parsed.resolution else parsed,
                file_quality,
                score,
                info,
                video.path,
            )
            await self._announce(title, dest, mf, rel, file_quality, mode, replaced)
            await self.remember_folder(title, show)
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

    async def remember_folder(self, title: Title, folder: str) -> None:
        """The title's own folder, once known (the first file filed or found in it):
        later files go there whatever the naming says. Never replaces one."""
        if title.folder or not folder:
            return
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(Title)
                .where(Title.id == title.id, Title.folder.is_(None))
                .values(folder=folder)
                .execution_options(synchronize_session=False)
            )
            await s.commit()
        title.folder = folder

    async def settle_found(
        self, title: Title, dest: Destination, folder: str | None, *, new: bool
    ) -> None:
        """A title found on disk (import of an existing library), in ``folder`` of
        ``dest``: a title added by that import lives in that library folder; its folder is
        remembered there, so later files join the ones already on disk."""
        default = await self.storage.default_destination(title.kind)
        home = title.destination_id or default.id
        if new and home != dest.id:
            async with self.db.sessionmaker() as s:
                await s.execute(
                    update(Title)
                    .where(Title.id == title.id)
                    .values(destination_id=dest.id)
                    .execution_options(synchronize_session=False)
                )
                await s.commit()
            title.destination_id = home = dest.id
        if home == dest.id and folder:
            await self.remember_folder(title, folder)

    async def title_folder(self, title_id: str) -> dict[str, Any]:
        """Where a title's files go: its library folder (its own, else the default for its
        kind) and its folder in there, named by that folder's naming settings. Movies and
        shows: ``Title (Year) {...}``; music: the artist's folder (or, with
        "Artist - Album (Year)" naming, the library folder itself: one folder per album)."""
        title = await self.media.get_title(title_id)
        own = bool(title.destination_id)
        dest = await (
            self.storage.destination(title.destination_id)
            if title.destination_id
            else self.storage.default_destination(title.kind)
        )
        sub = title.folder or await self._naming_folder(title, dest)
        if title.kind == "music" and self._flat_music():
            sub = None
        root = Path(dest.path)
        return {
            "library_folder": dest,
            "own": own,
            "remembered": bool(title.folder) and sub is not None,
            "path": str(root / sub) if sub else str(root),
        }

    async def change_folder(
        self, title_id: str, path: str, *, move: bool, actor: Actor
    ) -> dict[str, Any]:
        """Give a title another folder (a full path: its library folder is the parent).
        ``move``: what's in its current folder moves along (files a torrent seeds from
        through the engine), and the files Fetchbox recorded there point to the new place.
        Without it, files stay where they are; new ones go to the new folder.
        Returns {path, moved, failed: [{path, error}]}."""
        from fetchbox.core.paths import native_folder_problem
        from fetchbox.modules.media.service import _invalid

        title = await self.media.get_title(title_id)
        if title.kind == "music" and self._flat_music():
            raise _invalid(
                "body.path",
                "music is named Artist - Album (Year): there is no artist folder to choose",
            )
        p = Path(await asyncio.to_thread(os.path.expanduser, path.strip()))
        if not p.is_absolute() or p.parent == p:
            raise _invalid("body.path", "a full path, like /media/TV/The Bear")
        if why := native_folder_problem(str(p)):
            raise _invalid("body.path", why)
        if p.name != safe_component(p.name) or p.name in (".", ".."):
            raise _invalid("body.path", f"{p.name} can't be a folder name")
        old = await self.title_folder(title_id)
        old_dest: Destination = old["library_folder"]
        old_dir = Path(old["path"])
        dest_id = await self.storage.folder_for(
            title.kind, str(p.parent), field="body.path", actor=actor
        )
        new_dest = await (
            self.storage.destination(dest_id)
            if dest_id
            else self.storage.default_destination(title.kind)
        )
        new_root = Path(new_dest.path)
        moved = 0
        failed: list[dict[str, str]] = []
        if move and old_dir != p and await asyncio.to_thread(old_dir.is_dir):
            if p == old_dir.parent or old_dir in p.parents:
                raise _invalid("body.path", "can't move a folder into itself")
            async with self.db.sessionmaker() as s:
                recorded = {
                    str(Path(old_dest.path) / mf.relative_path): mf.id
                    for mf in (
                        await s.execute(
                            select(MediaFile)
                            .join(MediaFileItem, MediaFileItem.media_file_id == MediaFile.id)
                            .join(WantedItem, WantedItem.id == MediaFileItem.wanted_item_id)
                            .where(
                                WantedItem.title_id == title.id,
                                MediaFile.destination_id == old_dest.id,
                            )
                        )
                    ).scalars()
                }
            files = await asyncio.to_thread(_files_in, old_dir)
            for src in files:
                dst = p / src.relative_to(old_dir)
                try:
                    if not (
                        str(src) in recorded
                        and await self.downloads.move_seeding_file(str(src), str(dst))
                    ):
                        await asyncio.to_thread(_relocate, src, dst)
                except (OSError, DownloadClientError) as exc:
                    msg = getattr(exc, "message", None) or getattr(exc, "strerror", None) or exc
                    failed.append({"path": str(src), "error": str(msg)})
                    continue
                moved += 1
                if (mf_id := recorded.get(str(src))) is not None:
                    async with self.db.sessionmaker() as s:
                        mf = await s.get(MediaFile, mf_id)
                        if mf is not None:
                            mf.relative_path = dst.relative_to(new_root).as_posix()
                            mf.destination_id = new_dest.id
                            mf.storage_node_id = new_dest.node_id
                            await s.commit()
            await asyncio.to_thread(_prune_tree, old_dir, Path(old_dest.path))
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(Title)
                .where(Title.id == title.id)
                .values(destination_id=dest_id, folder=p.name, version=Title.version + 1)
                .execution_options(synchronize_session=False)
            )
            await s.commit()
        await self.bus.emit(
            "library.folder_changed",
            subject=Subject(type="title", id=title.id),
            actor=actor,
            path=str(p),
            moved=moved,
            failed=len(failed),
            folders=sorted({str(old_dir), str(p)}),  # for media servers to rescan
        )
        return {"path": str(p), "moved": moved, "failed": failed}

    def _flat_music(self) -> bool:
        """Music named "Artist - Album (Year)": one folder per album, no artist folder."""
        from fetchbox.modules.library import music_files as mfiles

        naming = self.music_naming() or mfiles.Naming()
        return bool(naming.folder == "artist - album (year)")

    async def _naming_folder(self, title: Title, dest: Destination) -> str | None:
        """The folder the naming gives a title in ``dest`` (None: music with no artist
        folder)."""
        if title.kind == "music":
            return None if self._flat_music() else safe_component(title.name) or None
        patterns = await self._patterns(dest)
        ctx = movie_context(title, parse_release(title.name or ""), "", None)
        ctx["TvdbId"] = title.tvdb_id
        key = "series_folder" if title.kind == "series" else "movie_folder"
        return safe_component(render(patterns[key], ctx)) or None

    async def folder_choices(self, title: Title, library_path: str | None) -> dict[str, Any]:
        """For a title being added (not saved): the library folder it would go to, the
        folder the naming would create in there, and folders already there that belong
        to it."""
        from fetchbox.modules.library.title_folders import folder_choices
        from fetchbox.modules.storage.service import _same_folder

        rows = await self.storage.destinations(title.kind)
        default = rows[0] if rows else await self.storage.default_destination(title.kind)
        path = (library_path or "").strip()
        dest = None
        for d in rows if path else []:
            if await asyncio.to_thread(_same_folder, d.path, path):
                dest = d
                break
        root = Path(path) if path and dest is None else Path((dest or default).path)
        # a new library folder gets the default's naming
        suggested = await self._naming_folder(title, dest or default)
        choices: list[dict[str, Any]] = []
        if suggested is not None:
            choices = await asyncio.to_thread(
                folder_choices,
                root,
                title.kind,
                name=title.name,
                year=title.year,
                tmdb_id=title.tmdb_id,
                tvdb_id=title.tvdb_id,
            )
        own = None
        if suggested is not None and not choices and root.parent != root:
            # the title's own folder typed as the library folder ("…/TV/Invincible"):
            # its parent is the library folder
            mine = await asyncio.to_thread(
                folder_choices,
                root.parent,
                title.kind,
                name=title.name,
                year=title.year,
                tmdb_id=title.tmdb_id,
                tvdb_id=title.tvdb_id,
            )
            own = next(
                ({**c, "library_folder": str(root.parent)} for c in mine if c["name"] == root.name),
                None,
            )
        return {
            "library_folder": str(root),
            "suggested": suggested,
            "choices": choices,
            "own": own,
        }

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
        """Put src at dst (see ``_put``), leaving no macOS ``._`` file beside it, nor one
        behind where it came from."""
        how = await self._put(src, dst, dest, d, rel)
        await asyncio.to_thread(_tidy_placed, src, dst)
        return how

    async def _put(
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

    # ---- sidecars: files that belong to a media file (see sidecars.py) ------------------

    async def add_sidecar(
        self,
        file_id: str,
        suffix: str,
        data: bytes,
        *,
        plugin: str,
        meta: dict[str, Any] | None = None,
    ) -> MediaFileSidecar:
        """Write ``data`` beside the file as ``<its name>.<suffix>`` and record it as the
        file's: from then on it's renamed, moved, recycled and deleted with the file."""
        check_suffix(suffix)
        f, _ = await self.file(file_id)
        if not f.destination_id:
            raise ConflictError(f"file {file_id} isn't in a library folder")
        dest = await self.storage.destination(f.destination_id)
        path = inside(Path(dest.path), sidecar_rel(f.relative_path, suffix))
        await asyncio.to_thread(write_atomically, path, data)
        async with self.db.sessionmaker() as s:
            row = await s.scalar(
                select(MediaFileSidecar).where(
                    MediaFileSidecar.media_file_id == file_id, MediaFileSidecar.suffix == suffix
                )
            )
            if row is None:
                row = MediaFileSidecar(media_file_id=file_id, suffix=suffix, plugin=plugin)
                s.add(row)
            row.plugin, row.meta = plugin, dict(meta or {})
            await s.commit()
            await s.refresh(row)
            return row

    async def sidecars(self, file_id: str) -> list[MediaFileSidecar]:
        async with self.db.sessionmaker() as s:
            rows = await s.execute(
                select(MediaFileSidecar)
                .where(MediaFileSidecar.media_file_id == file_id)
                .order_by(MediaFileSidecar.suffix)
            )
            return list(rows.scalars())

    async def remove_sidecar(self, file_id: str, suffix: str) -> bool:
        """Forget a sidecar and recycle its file (as the owner's folder recycles)."""
        f, _ = await self.file(file_id)
        async with self.db.sessionmaker() as s:
            row = await s.scalar(
                select(MediaFileSidecar).where(
                    MediaFileSidecar.media_file_id == file_id, MediaFileSidecar.suffix == suffix
                )
            )
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
        if f.destination_id:
            dest = await self.storage.destination(f.destination_id)
            await self._drop_sidecar_files(dest, f.relative_path, [suffix])
        return True

    async def _sidecar_suffixes(self, file_ids: list[str]) -> dict[str, list[str]]:
        if not file_ids:
            return {}
        async with self.db.sessionmaker() as s:
            rows = await s.execute(
                select(MediaFileSidecar.media_file_id, MediaFileSidecar.suffix).where(
                    MediaFileSidecar.media_file_id.in_(file_ids)
                )
            )
            out: dict[str, list[str]] = {}
            for fid, suffix in rows:
                out.setdefault(fid, []).append(suffix)
            return out

    async def _drop_sidecar_files(
        self, dest: Destination, owner_rel: str, suffixes: list[str]
    ) -> None:
        """Recycle (or delete) the sidecar files of ``owner_rel``; one that isn't there is
        skipped, and a failure never stops what the owner's change is doing."""
        recycle = str(Path(dest.path) / RECYCLE_DIR) if dest.recycle else None
        for suffix in suffixes:
            path = Path(dest.path) / sidecar_rel(owner_rel, suffix)
            if not await asyncio.to_thread(path.exists):
                continue
            try:
                await self.storage.local.delete(str(path), recycle_dir=recycle)
            except StorageError as exc:
                log.warning("sidecar_delete_failed", path=str(path), error=exc.message)

    async def _move_sidecar_files(
        self, root: Path, old_rel: str, new_rel: str, suffixes: list[str]
    ) -> list[str]:
        """Move the sidecars of a file renamed from ``old_rel`` to ``new_rel``. Returns the
        ones that couldn't move (they stay where they were)."""
        failed = []
        for suffix in suffixes:
            src = root / sidecar_rel(old_rel, suffix)
            dst = root / sidecar_rel(new_rel, suffix)
            if src == dst or not await asyncio.to_thread(src.exists):
                continue
            try:
                await asyncio.to_thread(_move, src, dst, root)
            except OSError as exc:
                log.warning("sidecar_move_failed", path=str(src), error=str(exc))
                failed.append(suffix)
        return failed

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
            suffixes = (await self._sidecar_suffixes([file_id])).get(file_id, [])
            await self._drop_sidecar_files(dest, f.relative_path, suffixes)
        async with self.db.sessionmaker() as s:
            row = await s.get(MediaFile, file_id)
            if row is not None:
                await s.delete(row)
            title_id: str | None = None
            for wid in items:
                w = await s.get(WantedItem, wid)
                if w is not None:
                    title_id = title_id or w.title_id
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
            title_id=title_id,
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
                failed.append(
                    {"file_id": p["file_id"], "current": p["current"], "error": exc.message}
                )
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
            if suffixes := (await self._sidecar_suffixes([p["file_id"]])).get(p["file_id"]):
                await self._move_sidecar_files(root, p["current"], p["new"], suffixes)
            renamed.append(p)
            await self.bus.emit(  # one per file (Activity skips it: library.renamed sums up)
                "file.renamed",
                subject=Subject(type="media_file", id=p["file_id"]),
                actor=actor,
                title_id=p.get("title_id"),
                kind=kind,
                old=p["current"],
                new=p["new"],
                destination_id=p.get("destination_id"),
            )
        # the titles now live where the naming puts them: their folder moves along
        moved = {p["title_id"]: p["new"].split("/")[0] for p in renamed if "/" in p["new"]}
        if moved:
            async with self.db.sessionmaker() as s:
                for tid, folder in moved.items():
                    await s.execute(
                        update(Title)
                        .where(Title.id == tid)
                        .values(folder=folder)
                        .execution_options(synchronize_session=False)
                    )
                await s.commit()
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


def _files_in(folder: Path) -> list[Path]:
    """Every file in a folder, recursively."""
    return sorted(Path(d) / f for d, _dirs, files in os.walk(folder) for f in files)


def _relocate(src: Path, dst: Path) -> None:
    """Move a file, never over another one; across disks it's copied, then removed."""
    if dst.exists() and not dst.samefile(src):
        raise FileExistsError(errno.EEXIST, f"{dst} already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copy2(src, dst)
        src.unlink()


def _prune_tree(folder: Path, root: Path) -> None:
    """Remove the empty folders in ``folder`` (deepest first), then ``folder`` and its
    parents while empty, up to ``root``."""
    for d, _dirs, _files in sorted(os.walk(folder), key=lambda w: -len(Path(w[0]).parts)):
        with contextlib.suppress(OSError):
            Path(d).rmdir()
    _prune(folder.parent, root)


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
