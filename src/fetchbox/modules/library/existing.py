"""Import an existing library: movies and shows already on disk, added to Fetchbox in place.

Nothing is changed until the user applies. A **scan** (a job) reads a library folder:
each top-level folder is one movie or one show (a loose video file in a movie folder is a
movie too). Each is matched on TMDB: an id in the name wins (``{tmdb-603}``,
``[tmdbid-603]``, ``{tvdb-371980}``), else the title and year. The result lists every
item with its files, the match, how sure it is, and whether Fetchbox already has it.

**Apply** (a job) adds the chosen titles without searching and records their files where
they are: a movie's biggest video file; a show's files matched to episodes by name. Each
matched wanted item becomes acquired (cutoff judged by the file's quality), so automatic
search only looks for what's missing or upgradable. Files are not moved or renamed; the
library settings' rename review does that when wanted.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable, Coroutine
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import structlog
from sqlalchemy import select

from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, Subject
from fetchbox.core.jobs import Job, JobContext, JobRunner
from fetchbox.modules.discovery.service import DiscoveryService
from fetchbox.modules.library.models import MediaFile
from fetchbox.modules.library.service import (
    _SAMPLE,
    VIDEO_EXTS,
    LibraryService,
    file_episodes,
)
from fetchbox.modules.media.models import Title
from fetchbox.modules.media.service import MediaService
from fetchbox.modules.releases.decision import title_key
from fetchbox.modules.releases.parser import parse_release
from fetchbox.modules.storage.service import RECYCLE_DIR, StorageService
from fetchbox.providers.storage import StorageError
from fetchbox.providers.storage.local import inside

log = structlog.get_logger(__name__)

SCAN_JOB = "library.scan"
APPLY_JOB = "library.import_existing"
LOOKUPS_AT_ONCE = 4
# Adding many titles at once mustn't start hundreds of searches: the first search waits.
FIRST_SEARCH_AFTER = timedelta(hours=1)

_TMDB = re.compile(r"[\[{(]\s*tmdb(?:id)?[-=: ]?(\d+)\s*[\]})]", re.I)
_TVDB = re.compile(r"[\[{(]\s*tvdb(?:id)?[-=: ]?(\d+)\s*[\]})]", re.I)
_IDS = re.compile(r"[\[{(]\s*(?:tmdb|tvdb|imdb)(?:id)?[-=: ]?[a-z0-9]+\s*[\]})]", re.I)
_TITLE_YEAR = re.compile(r"^(?P<title>.+?)[\s._-]*[(\[](?P<year>(?:19|20)\d\d)[)\]]")


def folder_hints(name: str) -> dict[str, Any]:
    """What a folder (or file) name says: tmdb/tvdb ids, title, year."""
    out: dict[str, Any] = {}
    if m := _TMDB.search(name):
        out["tmdb_id"] = int(m.group(1))
    if m := _TVDB.search(name):
        out["tvdb_id"] = int(m.group(1))
    bare = _IDS.sub("", name).strip()
    if m := _TITLE_YEAR.match(bare):
        out["title"], out["year"] = m.group("title").strip(" ._-"), int(m.group("year"))
    else:
        p = parse_release(bare)
        out["title"], out["year"] = p.title or bare, p.year
    return out


def _is_video(name: str) -> bool:
    return PurePosixPath(name).suffix.lower().lstrip(".") in VIDEO_EXTS and not name.startswith(
        "._"
    )


def _scan_tree(root: Path, kind: str) -> list[dict[str, Any]]:
    """Top-level items of a library folder with their video files (no samples)."""
    items: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), key=lambda e: str(e.name).lower()):
        if entry.name.startswith(".") or entry.name == RECYCLE_DIR:
            continue
        if entry.is_file():
            if kind == "movie" and _is_video(entry.name) and not _SAMPLE.search(entry.name):
                items.append(
                    {"folder": entry.name, "files": [{"path": entry.name,
                                                      "size": entry.stat().st_size}]}
                )  # fmt: skip
            continue
        if not entry.is_dir():
            continue
        files: list[dict[str, Any]] = []
        for dirpath, dirnames, filenames in os.walk(entry):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and not _SAMPLE.search(d)]
            for f in filenames:
                if _is_video(f) and not _SAMPLE.search(f):
                    full = Path(dirpath) / f
                    files.append(
                        {"path": full.relative_to(root).as_posix(), "size": full.stat().st_size}
                    )
        if files:
            files.sort(key=lambda f: f["path"])
            items.append({"folder": entry.name, "files": files})
    return items


class ExistingLibrary:
    def __init__(
        self,
        db: Database,
        jobs: JobRunner,
        *,
        storage: StorageService,
        media: MediaService,
        discovery: DiscoveryService,
        library: LibraryService,
    ) -> None:
        self.db = db
        self.jobs = jobs
        self.storage = storage
        self.media = media
        self.discovery = discovery
        self.library = library
        self.music: Any = None  # ExistingMusic, set by the app (music folders)

    def register_jobs(self) -> None:
        @self.jobs.handler(SCAN_JOB, queue="net", max_attempts=1)
        async def scan(ctx: JobContext) -> dict[str, Any]:
            return await self.scan(ctx.payload["destination_id"], progress=ctx.progress)

        @self.jobs.handler(APPLY_JOB, queue="net", max_attempts=1)
        async def apply(ctx: JobContext) -> dict[str, Any]:
            p = ctx.payload
            return await self.apply(
                p["destination_id"],
                p["items"],
                actor=Actor(type=p["actor"]["type"], id=p["actor"].get("id")),
                progress=ctx.progress,
            )

    async def start_scan(self, destination_id: str) -> Job:
        await self.storage.destination(destination_id)  # 404 if unknown
        job = await self.jobs.enqueue(
            SCAN_JOB, {"destination_id": destination_id}, dedupe_key=f"scan:{destination_id}"
        )
        if job is None:
            raise ConflictError("this folder is already being scanned")
        return job

    async def start_apply(
        self, destination_id: str, items: list[dict[str, Any]], *, actor: Actor
    ) -> Job:
        await self.storage.destination(destination_id)
        job = await self.jobs.enqueue(
            APPLY_JOB,
            {"destination_id": destination_id, "items": items,
             "actor": {"type": actor.type, "id": actor.id}},
            dedupe_key=f"import-existing:{destination_id}",
        )  # fmt: skip
        if job is None:
            raise ConflictError("an import into this folder is already running")
        return job

    # ---- scan ---------------------------------------------------------------------------------

    async def scan(
        self,
        destination_id: str,
        *,
        progress: Callable[[float, str | None], Coroutine[Any, Any, None]] | None = None,
    ) -> dict[str, Any]:
        dest = await self.storage.destination(destination_id)
        kind = dest.media_kind
        root = Path(dest.path)
        if kind == "music" and self.music is not None:
            if not await asyncio.to_thread(root.is_dir):
                raise ProblemError(
                    422, "Validation Failed", f"{root} is not a folder", slug="validation"
                )
            result: dict[str, Any] = await self.music.scan(dest, progress)
            return result
        if not await asyncio.to_thread(root.is_dir):
            raise ProblemError(
                422, "Validation Failed", f"{root} is not a folder", slug="validation"
            )
        items = await asyncio.to_thread(_scan_tree, root, kind)
        async with self.db.sessionmaker() as s:
            known = {
                t.tmdb_id: t
                for t in (await s.execute(select(Title).where(Title.kind == kind))).scalars()
                if t.tmdb_id
            }
            recorded = set(
                (
                    await s.execute(
                        select(MediaFile.relative_path).where(MediaFile.destination_id == dest.id)
                    )
                ).scalars()
            )
        sem = asyncio.Semaphore(LOOKUPS_AT_ONCE)
        done = 0

        async def one(item: dict[str, Any]) -> None:
            nonlocal done
            async with sem:
                await self._match(item, kind)
            m = item["match"]
            title = known.get(m["tmdb_id"]) if m else None
            item["title_id"] = title.id if title else None
            paths = [f["path"] for f in item["files"]]
            item["status"] = (
                "imported" if all(p in recorded for p in paths)
                else "in_library" if title else "new"
            )  # fmt: skip
            item["size"] = sum(f["size"] for f in item["files"])
            biggest = max(item["files"], key=lambda f: f["size"])
            item["quality"] = parse_release(PurePosixPath(biggest["path"]).name).quality
            done += 1
            if progress and (done % 10 == 0 or done == len(items)):
                await progress(done / max(len(items), 1), f"{done} of {len(items)} matched")

        await asyncio.gather(*(one(i) for i in items))
        return {
            "destination_id": dest.id,
            "kind": kind,
            "path": dest.path,
            "items": items,
            "counts": {
                s: sum(i["status"] == s for i in items) for s in ("new", "in_library", "imported")
            },
        }

    async def _match(self, item: dict[str, Any], kind: str) -> None:
        """Set item["match"] (a TMDB summary or None) and item["confidence"]."""
        hints = folder_hints(item["folder"] if kind == "series" or "/" in item["files"][0]["path"]
                             else PurePosixPath(item["folder"]).stem)  # fmt: skip
        item["hints"] = hints
        item["match"], item["confidence"] = None, "none"
        try:
            if hints.get("tmdb_id"):
                d = await (
                    self.discovery.movie(hints["tmdb_id"])
                    if kind == "movie"
                    else self.discovery.series(hints["tmdb_id"])
                )
                item["match"], item["confidence"] = _summary(d), "exact"
                return
            if not hints.get("title"):
                return
            found = await self.discovery.search(hints["title"], kind, hints.get("year"))  # type: ignore[arg-type]
            if not found and hints.get("year"):
                found = await self.discovery.search(hints["title"], kind, None)  # type: ignore[arg-type]
            if not found:
                return
            if hints.get("tvdb_id") and kind == "series":
                for cand in found[:3]:
                    d = await self.discovery.series(cand.tmdb_id)
                    if d.tvdb_id == hints["tvdb_id"]:
                        item["match"], item["confidence"] = _summary(d), "exact"
                        return
            best = found[0]
            same_title = title_key(best.name) == title_key(hints["title"]) or (
                best.original_name and title_key(best.original_name) == title_key(hints["title"])
            )
            year_ok = (
                hints.get("year") is None
                or best.year is None
                or abs(best.year - hints["year"]) <= (1 if kind == "movie" else 0)
            )
            item["match"] = _summary(best)
            item["confidence"] = "high" if same_title and year_ok and hints.get("year") else "low"
        except (ProblemError, NotFoundError) as exc:
            item["error"] = getattr(getattr(exc, "problem", None), "detail", None) or str(exc)

    # ---- apply --------------------------------------------------------------------------------

    async def apply(
        self,
        destination_id: str,
        items: list[dict[str, Any]],
        *,
        actor: Actor,
        progress: Callable[[float, str | None], Coroutine[Any, Any, None]] | None = None,
    ) -> dict[str, Any]:
        """Each item: {folder, files: [paths], tmdb_id, profile_id, monitor_mode (series) or
        monitored (movies)}."""
        dest = await self.storage.destination(destination_id)
        kind = dest.media_kind
        added: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        files_recorded = 0
        later = utcnow() + FIRST_SEARCH_AFTER
        for n, item in enumerate(items, 1):
            try:
                if kind == "music":
                    title, new, count = await self.music.apply_one(dest, item, actor)
                    # Artist/Album (Year)/...: the artist's folder
                    parts = PurePosixPath(item["folder"]).parts
                    found = parts[0] if len(parts) > 1 else None
                else:
                    title, new = await self._title(kind, item, actor, later)
                    count = await self._adopt_files(title, dest, item["files"])
                    # a folder (not a loose movie file in the library folder itself)
                    files = [f if isinstance(f, str) else f["path"] for f in item["files"]]
                    found = item["folder"] if any("/" in f for f in files) else None
                if count:
                    await self.library.settle_found(title, dest, found, new=new)
                files_recorded += count
                added.append({"folder": item["folder"], "title_id": title.id,
                              "name": title.name, "new": new, "files": count})  # fmt: skip
            except ProblemError as exc:
                skipped.append({"folder": item["folder"], "reason": exc.problem.detail})
            except Exception as exc:  # one bad folder mustn't stop the rest
                log.exception("library.import_existing_failed", folder=item["folder"])
                skipped.append({"folder": item["folder"], "reason": f"{type(exc).__name__}: {exc}"})
            if progress:
                await progress(n / len(items), f"{n} of {len(items)}")
        if files_recorded:
            await self.library.bus.emit(
                "library.imported_existing",
                subject=Subject(type="destination", id=dest.id),
                actor=actor,
                count=len(added),
                files=files_recorded,
                folders=sorted({str(Path(dest.path) / i["folder"]) for i in items
                                if any(a["folder"] == i["folder"] for a in added)}),
            )  # fmt: skip
        return {"added": added, "skipped": skipped, "files": files_recorded}

    async def _title(
        self, kind: str, item: dict[str, Any], actor: Actor, later: Any
    ) -> tuple[Title, bool]:
        async with self.db.sessionmaker() as s:
            existing = (
                await s.execute(
                    select(Title).where(Title.kind == kind, Title.tmdb_id == item["tmdb_id"])
                )
            ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        if kind == "movie":
            t = await self.media.add_movie(
                int(item["tmdb_id"]), profile_id=item["profile_id"],
                monitored=bool(item.get("monitored", True)), actor=actor, search_after=later,
            )  # fmt: skip
        else:
            t = await self.media.add_series(
                int(item["tmdb_id"]), profile_id=item["profile_id"],
                monitor_mode=item.get("monitor_mode") or "all", actor=actor, search_after=later,
            )  # fmt: skip
        return t, True

    async def _adopt_files(self, title: Title, dest: Any, paths: list[str]) -> int:
        """Record files already in the library folder for this title's wanted items."""
        root = Path(dest.path)
        if title.kind == "movie":
            sizes: dict[str, int] = {}
            for p in paths:
                try:
                    full = inside(root, p)  # paths come from the API: stay in the folder
                except StorageError:
                    continue
                if (n := await asyncio.to_thread(_size, full)) is not None:
                    sizes[p] = n
            if not sizes:
                return 0
            biggest = max(sizes, key=lambda p: sizes[p])
            wanted = (await self.media.wanted_for([title.id])).get(title.id, [])
            return int(await self.library.adopt(title, dest, biggest, wanted))
        rows = await self.media.episode_items(title.id)
        by_key = {(n, ep.number): w for n, ep, w in rows}
        by_air = {ep.air_date: (n, ep.number) for n, ep, _ in rows if ep.air_date}
        by_abs = {ep.absolute_number: (n, ep.number) for n, ep, _ in rows if ep.absolute_number}
        count = 0
        for p in paths:
            keys = [k for k in file_episodes(p, by_air, by_abs) if k in by_key]
            if keys and await self.library.adopt(title, dest, p, [by_key[k] for k in keys]):
                count += 1
        return count


def _size(p: Path) -> int | None:
    try:
        return p.stat().st_size
    except OSError:
        return None


def _summary(d: Any) -> dict[str, Any]:
    return {
        "tmdb_id": d.tmdb_id,
        "name": d.name,
        "year": d.year,
        "poster_url": d.poster_url,
        "overview": (d.overview or "")[:300],
    }
