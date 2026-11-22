"""Move a library folder somewhere else, with the files Fetchbox recorded in it.

Changing a library folder's path only changes the setting; this moves the library too
(e.g. from WSL's own disk to the drive the downloads are on, so imports become
hardlinks instead of copies). Safe across drives:

1. copy every recorded file to the new folder and check each copy's size (a file that
   is already there with the same size is kept); on any failure the copies made so far
   are removed and nothing else changes;
2. switch the folder's path;
3. delete the old copies, and the old folder's emptied subfolders.

Only files Fetchbox recorded move; anything else in the old folder stays where it is.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import select

from fetchbox.core.db import Database
from fetchbox.core.errors import ConflictError, ProblemError
from fetchbox.core.events import Actor
from fetchbox.core.jobs import Job, JobContext, JobRunner
from fetchbox.modules.library.models import MediaFile
from fetchbox.modules.storage.service import StorageService

MOVE_JOB = "library.move_folder"


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".fetchbox-partial")
    shutil.copy2(src, part)
    if part.stat().st_size != src.stat().st_size:
        part.unlink(missing_ok=True)
        raise OSError(f"the copy of {src.name} came out the wrong size")
    os.replace(part, dst)


def _same_size(src: Path, dst: Path) -> bool:
    return dst.is_file() and dst.stat().st_size == src.stat().st_size


def _prune_empty(root: Path, start: Path) -> None:
    """Remove now-empty folders from ``start`` up to (not including) ``root``."""
    p = start
    while p != root and root in p.parents:
        try:
            p.rmdir()
        except OSError:
            return
        p = p.parent


class FolderMover:
    def __init__(self, db: Database, jobs: JobRunner, storage: StorageService) -> None:
        self.db = db
        self.jobs = jobs
        self.storage = storage

    def register_jobs(self) -> None:
        @self.jobs.handler(MOVE_JOB, queue="maintenance", max_attempts=1)
        async def run(ctx: JobContext) -> dict[str, Any]:
            p = ctx.payload
            return await self.move(p["destination_id"], p["path"], progress=ctx.progress)

    async def start(self, dest_id: str, path: str, actor: Actor) -> Job:
        d = await self.storage.destination(dest_id)
        new = Path(path)
        if not new.is_absolute():
            raise ProblemError(422, "Validation Failed", "the new folder must be an absolute path",
                               slug="validation")  # fmt: skip
        old = Path(d.path)
        if new == old or old in new.parents or new in old.parents:
            raise ProblemError(422, "Validation Failed",
                               "the new folder can't be the old one, inside it, or around it",
                               slug="validation")  # fmt: skip
        job = await self.jobs.enqueue(
            MOVE_JOB, {"destination_id": dest_id, "path": str(new)}, dedupe_key=f"move:{dest_id}"
        )
        if job is None:
            raise ConflictError("this library folder is already being moved")
        return job

    async def move(self, dest_id: str, path: str, *, progress: Any = None) -> dict[str, Any]:
        d = await self.storage.destination(dest_id)
        old, new = Path(d.path), Path(path)
        async with self.db.sessionmaker() as s:
            files = list(
                (
                    await s.execute(select(MediaFile).where(MediaFile.destination_id == dest_id))
                ).scalars()
            )
        todo = [(old / f.relative_path, new / f.relative_path) for f in files]
        present = [(a, b) for a, b in todo if await asyncio.to_thread(a.is_file)]
        missing = len(todo) - len(present)
        made: list[Path] = []
        try:
            await asyncio.to_thread(new.mkdir, parents=True, exist_ok=True)
            for n, (src, dst) in enumerate(present, 1):
                same = await asyncio.to_thread(_same_size, src, dst)
                if not same:
                    await asyncio.to_thread(_copy, src, dst)
                    made.append(dst)
                if progress:
                    await progress(n / max(len(present), 1) * 0.9, f"copied {n} of {len(present)}")
        except OSError as exc:
            for p in made:  # leave nothing half-done behind
                await asyncio.to_thread(p.unlink, True)
            raise ProblemError(
                409, "Conflict", f"couldn't copy into {new}: {exc}; nothing was changed",
                slug="conflict",
            ) from None  # fmt: skip

        await self.storage.update_destination(
            dest_id, {"path": str(new)}, if_match=None, actor=Actor(type="system")
        )
        removed = 0
        for src, _ in present:
            try:
                await asyncio.to_thread(src.unlink)
                removed += 1
                await asyncio.to_thread(_prune_empty, old, src.parent)
            except OSError:
                pass  # the copy is in place; a leftover old copy is only wasted space
        if progress:
            await progress(1.0, "done")
        return {"moved": len(present), "missing": missing, "old_copies_removed": removed,
                "path": str(new)}  # fmt: skip
