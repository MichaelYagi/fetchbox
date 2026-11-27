"""Library check: missing files, duplicates, and files Fetchbox doesn't know about.

A check (a job) only reads, and reports three things:

- **missing**: a file Fetchbox recorded is no longer on disk (in a library folder that is
  online, so an unplugged NAS isn't reported as hundreds of missing files);
- **duplicates**: the same file, by size and quick hash, recorded more than once across
  all library folders. Reported, never deleted (spec: "Filesystem safety");
- **unknown**: video files in library folders that no record points to (bring them in
  with the existing-library import).

Quick hashes missing on records (files adopted by the existing-library import, which
doesn't read whole files) are computed and stored along the way.

"Forget" is the one action: drop records of missing files, so their movies and
episodes are wanted (and searched for) again.
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from collections.abc import Callable, Coroutine
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update

from fetchbox.core.db import Database
from fetchbox.core.errors import ConflictError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.jobs import Job, JobContext, JobRunner
from fetchbox.core.providers import HealthStatus
from fetchbox.core.schedules import ScheduleDef, Scheduler
from fetchbox.modules.library.models import MediaFile, MediaFileItem
from fetchbox.modules.library.service import _SAMPLE, is_video
from fetchbox.modules.media.models import HistoryEvent, Title, WantedItem
from fetchbox.modules.storage.service import RECYCLE_DIR, StorageService
from fetchbox.providers.storage.local import quick_hash

CHECK_JOB = "library.check"
UNKNOWN_LIMIT = 500  # listed; the count is always complete

Progress = Callable[[float, str | None], Coroutine[Any, Any, None]]


def _size(p: Path) -> int | None:
    try:
        return p.stat().st_size
    except OSError:
        return None


def _videos(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != RECYCLE_DIR]
        for f in filenames:
            if is_video(f) and not _SAMPLE.search(f):
                out.append((Path(dirpath) / f).relative_to(root).as_posix())
    return out


class LibraryCheck:
    def __init__(
        self, db: Database, bus: EventBus, jobs: JobRunner, storage: StorageService
    ) -> None:
        self.db = db
        self.bus = bus
        self.jobs = jobs
        self.storage = storage

    def register_jobs(self, scheduler: Scheduler | None = None) -> None:
        @self.jobs.handler(CHECK_JOB, queue="maintenance", max_attempts=1)
        async def run(ctx: JobContext) -> dict[str, Any]:
            return await self.check(progress=ctx.progress)

        if scheduler is not None:  # daily, only reporting: Health says what's missing
            scheduler.define(
                ScheduleDef(
                    "library_check", CHECK_JOB, timedelta(days=1),
                    "Check the library for files missing from disk (changes nothing)",
                )
            )  # fmt: skip

    async def latest(self) -> dict[str, Any] | None:
        """The last finished check's report, without the missing files dealt with since
        (forgotten, deleted, or their movie removed)."""
        async with self.db.sessionmaker() as s:
            job = await s.scalar(
                select(Job)
                .where(Job.type == CHECK_JOB, Job.status == "succeeded")
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(1)
            )
            if job is None or not job.result:
                return None
            report = dict(job.result)
            ids = [m["file_id"] for m in report.get("missing") or []]
            still = set(
                (await s.execute(select(MediaFile.id).where(MediaFile.id.in_(ids)))).scalars()
            ) if ids else set()  # fmt: skip
        report["missing"] = [m for m in report.get("missing") or [] if m["file_id"] in still]
        report["checked_at"] = (job.finished_at or job.created_at).isoformat()
        return report

    async def health(self) -> HealthStatus:
        report = await self.latest()
        missing = len(report["missing"]) if report else 0
        if missing:
            return HealthStatus(
                status="warning",
                message=f"{missing} file{'s' if missing != 1 else ''} missing from disk "
                "(found by the library check)",
            )
        return HealthStatus(status="ok", message="No files missing from disk")

    async def start(self) -> Job:
        job = await self.jobs.enqueue(CHECK_JOB, {}, dedupe_key="library-check")
        if job is None:
            raise ConflictError("a library check is already running")
        return job

    async def check(self, *, progress: Progress | None = None) -> dict[str, Any]:
        dests = {d.id: d for d in await self.storage.destinations()}
        online = {i for i, d in dests.items() if d.status in ("online", "degraded", "low_space")}
        async with self.db.sessionmaker() as s:
            files = list((await s.execute(select(MediaFile))).scalars())
            links = (
                await s.execute(
                    select(MediaFileItem.media_file_id, Title.id, Title.name)
                    .join(WantedItem, WantedItem.id == MediaFileItem.wanted_item_id)
                    .join(Title, Title.id == WantedItem.title_id)
                )
            ).all()
        title_of = {fid: (tid, name) for fid, tid, name in links}

        missing: list[dict[str, Any]] = []
        hashes: dict[str, str] = {}
        skipped = 0
        for n, f in enumerate(files, 1):
            d = dests.get(f.destination_id or "")
            if d is None or d.id not in online:
                skipped += 1  # offline / removed folder: can't tell
                continue
            path = Path(d.path) / f.relative_path
            size = await asyncio.to_thread(_size, path)
            tid, name = title_of.get(f.id, (None, None))
            if size is None:
                missing.append({"file_id": f.id, "path": str(path), "title_id": tid,
                                "title": name})  # fmt: skip
                continue
            if f.quick_hash is None:
                try:
                    hashes[f.id] = await asyncio.to_thread(quick_hash, path)
                except OSError:
                    continue
            if progress and n % 25 == 0:
                await progress(n / max(len(files), 1) * 0.8, f"{n} of {len(files)} files")
        if hashes:
            async with self.db.sessionmaker() as s:
                for fid, h in hashes.items():
                    await s.execute(
                        update(MediaFile).where(MediaFile.id == fid).values(quick_hash=h)
                        .execution_options(synchronize_session=False)
                    )  # fmt: skip
                await s.commit()

        groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        missing_ids = {m["file_id"] for m in missing}
        for f in files:
            qh = hashes.get(f.id) or f.quick_hash
            d = dests.get(f.destination_id or "")
            if not qh or d is None or f.id in missing_ids:
                continue
            tid, name = title_of.get(f.id, (None, None))
            where = str(Path(d.path) / f.relative_path)
            groups[(f.size, qh)].append(
                {"file_id": f.id, "path": where, "title_id": tid, "title": name, "size": f.size}
            )
        duplicates = [g for g in groups.values() if len(g) > 1]

        known = defaultdict(set)
        for f in files:
            if f.destination_id:
                known[f.destination_id].add(f.relative_path)
        unknown: list[dict[str, str]] = []
        unknown_count = 0
        for d in dests.values():
            if d.id not in online:
                continue
            for rel in await asyncio.to_thread(_videos, Path(d.path)):
                if rel not in known[d.id]:
                    unknown_count += 1
                    if len(unknown) < UNKNOWN_LIMIT:
                        unknown.append({"destination_id": d.id, "folder": d.name,
                                        "path": str(Path(d.path) / rel)})  # fmt: skip
        if progress:
            await progress(1.0, "done")
        return {
            "files": len(files),
            "not_checked": skipped,
            "missing": missing,
            "duplicates": duplicates,
            "unknown": unknown,
            "unknown_count": unknown_count,
            "hashed": len(hashes),
        }

    async def forget(self, file_ids: list[str], *, actor: Actor) -> int:
        """Drop the records of missing files (only those really missing); their movies
        and episodes become wanted again and are searched for."""
        dests = {d.id: d for d in await self.storage.destinations()}
        async with self.db.sessionmaker() as s:
            rows = list(
                (await s.execute(select(MediaFile).where(MediaFile.id.in_(file_ids)))).scalars()
            )
            gone = []
            for f in rows:
                d = dests.get(f.destination_id or "")
                if d is None or d.status not in ("online", "degraded", "low_space"):
                    continue  # can't confirm it's missing
                if await asyncio.to_thread(_size, Path(d.path) / f.relative_path) is None:
                    gone.append(f.id)
            if not gone:
                return 0
            items = list(
                (
                    await s.execute(
                        select(WantedItem)
                        .join(MediaFileItem, MediaFileItem.wanted_item_id == WantedItem.id)
                        .where(MediaFileItem.media_file_id.in_(gone))
                    )
                ).scalars()
            )
            await s.execute(delete(MediaFileItem).where(MediaFileItem.media_file_id.in_(gone)))
            await s.execute(delete(MediaFile).where(MediaFile.id.in_(gone)))
            for w in items:
                w.state, w.lock, w.cutoff_met, w.next_search_at = "missing", None, False, None
                s.add(HistoryEvent(wanted_item_id=w.id, title_id=w.title_id, type="deleted",
                                   data={"reason": "the file is missing from disk"}))  # fmt: skip
            await s.commit()
        await self.bus.emit(
            "library.missing_forgotten",
            subject=Subject(type="library", id="files"),
            actor=actor,
            count=len(gone),
        )
        return len(gone)
