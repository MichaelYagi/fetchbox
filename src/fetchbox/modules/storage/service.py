"""Storage nodes and destinations: seeding, health checks, CRUD.

First run creates one ``local`` node for this computer and two default destinations
under the downloads root (``Movies`` and ``TV``). Each destination is checked: the
folder exists (created if it's a default one), a probe file can be written and
deleted, hardlinks work from the torrent folder, and free space is above the floor
(default: the larger of 5% and 20 GB, capped at half the disk). Transitions emit
``storage.online`` / ``storage.offline`` / ``storage.low_space``.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from fetchbox.core.concurrency import check_if_match
from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, FieldError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.jobs import JobContext, JobRunner
from fetchbox.core.paths import native_folder_problem
from fetchbox.core.providers import HealthStatus
from fetchbox.core.schedules import ScheduleDef, Scheduler
from fetchbox.modules.storage.models import LINK_MODES, Destination, StorageNode
from fetchbox.providers.storage import StorageError
from fetchbox.providers.storage.local import LocalStorage, cleanup_partials

log = structlog.get_logger(__name__)

LOCAL_NODE = "This computer"
GB = 1_000_000_000
FLOOR_MIN = 20 * GB
FLOOR_SHARE = 0.05
RECYCLE_DIR = ".fetchbox-recycle"
RECYCLE_JOB = "storage.recycle_cleanup"


def _invalid(field: str, message: str) -> ProblemError:
    return ProblemError(
        422,
        "Validation Failed",
        slug="validation",
        errors=[FieldError(field=field, message=message)],
    )


def _days(bin_: Path) -> list[Path]:
    return sorted(bin_.iterdir()) if bin_.is_dir() else []


class _Unmounted(Exception):
    def __init__(self, mount: str) -> None:
        super().__init__(mount)
        self.mount = mount


def mount_of(path: Path) -> str | None:
    """The mount point holding ``path`` (or its nearest existing parent)."""
    p = Path(os.path.abspath(path))
    while not p.exists() and p != p.parent:
        p = p.parent
    while True:
        if os.path.ismount(p):
            return str(p)
        if p == p.parent:
            return None
        p = p.parent


def _is_root(mount: str) -> bool:
    """The system's own root: nothing to watch (a missing root means no Fetchbox either)."""
    return mount in ("/", os.path.abspath(os.sep)) or (
        len(mount) <= 3 and mount[1:2] == ":" and mount[:1].upper() == "C"
    )


NETWORK_FS = {"nfs", "nfs4", "cifs", "smb", "smb3", "smbfs", "afpfs", "fuse.sshfs", "davfs",
              "fuse.rclone", "9p"}  # fmt: skip


def mounts() -> list[dict[str, Any]]:
    """Drives and shares mounted on this computer, for picking a library folder."""
    import psutil

    out = []
    for part in psutil.disk_partitions(all=True):
        mp = part.mountpoint
        if mp.startswith(("/proc", "/sys", "/dev", "/run", "/snap", "/boot", "/var/lib", "/usr")):
            continue
        network = part.fstype in NETWORK_FS or part.device.startswith(("//", "\\\\"))
        local_disk = part.device.startswith(("/dev/", "C:", "D:", "E:", "F:")) or len(mp) <= 3
        if not (network or local_disk or part.fstype in ("drvfs", "ntfs", "exfat", "vfat")):
            continue
        try:
            u = psutil.disk_usage(mp)
        except OSError:
            continue
        out.append({"path": mp, "device": part.device, "fstype": part.fstype,
                    "network": network, "total": u.total, "free": u.free})  # fmt: skip
    return sorted(out, key=lambda m: m["path"])


def _norm_folder(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expanduser(path)))


def _same_folder(a: str, b: str) -> bool:
    return _norm_folder(a) == _norm_folder(b)


def _can_write(folder: Path) -> str | None:
    """None if ``folder`` exists or can be created, and can be written to; else why not."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".fetchbox-write-test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        return f"can't use {folder}: {exc.strerror or exc}"
    return None


def _has_files(folder: Path) -> bool:
    """Any file in ``folder`` (its recycle folder included); a missing folder has none."""
    try:
        return any(p.is_file() for p in folder.rglob("*"))
    except OSError:
        return True  # can't tell: leave it alone


def default_floor(total: int) -> int:
    return min(max(int(total * FLOOR_SHARE), FLOOR_MIN), total // 2)


class StorageService:
    def __init__(
        self, db: Database, bus: EventBus, *, downloads_root: Path, torrents_dir: Path
    ) -> None:
        self.db = db
        self.bus = bus
        self.downloads_root = downloads_root
        self.torrents_dir = torrents_dir
        self.local = LocalStorage(LocalStorage.Settings())

    # ---- seed + startup ---------------------------------------------------------------------

    async def seed(self, naming_template_id: str | None) -> None:
        async with self.db.sessionmaker() as s:
            node = await s.scalar(select(StorageNode).where(StorageNode.kind == "local"))
            if node is None:
                node = StorageNode(kind="local", name=LOCAL_NODE)
                s.add(node)
                await s.flush()
            if not await s.scalar(select(Destination.id).limit(1)):
                for kind, folder in (("movie", "Movies"), ("series", "TV")):
                    s.add(
                        Destination(
                            node_id=node.id,
                            name=folder,
                            media_kind=kind,
                            path=str(self.downloads_root / folder),
                            is_default=True,
                            naming_template_id=naming_template_id,
                        )
                    )
                await s.flush()
            # Music (also on databases from before it): next to the default movie folder,
            # so it's on the same drive as the rest of the library
            if not await s.scalar(
                select(Destination.id).where(Destination.media_kind == "music").limit(1)
            ):
                movies = await s.scalar(
                    select(Destination).where(
                        Destination.media_kind == "movie", Destination.is_default.is_(True)
                    )
                )
                base = Path(movies.path).parent if movies else self.downloads_root
                name = "Music"
                taken = set((await s.execute(select(Destination.name))).scalars())
                n = 1
                while name in taken:
                    n += 1
                    name = f"Music {n}"
                s.add(
                    Destination(
                        node_id=node.id,
                        name=name,
                        media_kind="music",
                        path=str(base / "Music"),
                        is_default=True,
                    )
                )
            await s.commit()
        await self._repoint_crossing_defaults()

    async def _repoint_crossing_defaults(self) -> None:
        """A default folder Fetchbox chose itself on the other system (an earlier version
        under WSL picked the Windows Downloads folder) moves to this system's default,
        but only while nothing is in it: no files filed there, nothing downloading into
        it, nothing on disk. A folder with files in it stays, flagged for you to change."""
        from fetchbox.modules.downloads.models import Download
        from fetchbox.modules.library.models import MediaFile

        async with self.db.sessionmaker() as s:
            defaults = list(
                (
                    await s.execute(select(Destination).where(Destination.is_default.is_(True)))
                ).scalars()
            )
            taken = {d.path for d in (await s.execute(select(Destination))).scalars()}
            changed = False
            for d in defaults:
                if not native_folder_problem(d.path):
                    continue
                target = str(self.downloads_root / Path(d.path).name)
                if target in taken or native_folder_problem(target):
                    continue
                if await s.scalar(
                    select(MediaFile.id).where(MediaFile.destination_id == d.id).limit(1)
                ):
                    continue
                if await s.scalar(
                    select(Download.id)
                    .where(Download.status != "removed", Download.save_path.startswith(d.path))
                    .limit(1)
                ):
                    continue
                if await asyncio.to_thread(_has_files, Path(d.path)):
                    continue
                log.info("storage.default_folder_moved", folder=d.name, old=d.path, new=target)
                taken.discard(d.path)
                taken.add(target)
                d.path = target
                changed = True
            if changed:
                await s.commit()

    async def recover(self) -> list[str]:
        """Roll back file operations a crash interrupted (partial files)."""
        removed: list[str] = []
        for d in await self.destinations():
            removed += cleanup_partials(d.path)
        if removed:
            log.warning("storage.partials_removed", files=removed)
        return removed

    # ---- queries ----------------------------------------------------------------------------

    async def nodes(self) -> list[StorageNode]:
        async with self.db.sessionmaker() as s:
            return list((await s.execute(select(StorageNode).order_by(StorageNode.name))).scalars())

    async def node(self, node_id: str) -> StorageNode:
        async with self.db.sessionmaker() as s:
            row = await s.get(StorageNode, node_id)
        if row is None:
            raise NotFoundError("storage node", node_id)
        return row

    async def destinations(self, media_kind: str | None = None) -> list[Destination]:
        async with self.db.sessionmaker() as s:
            q = select(Destination).order_by(Destination.is_default.desc(), Destination.name)
            if media_kind:
                q = q.where(Destination.media_kind == media_kind)
            return list((await s.execute(q)).scalars())

    async def destination(self, dest_id: str) -> Destination:
        async with self.db.sessionmaker() as s:
            row = await s.get(Destination, dest_id)
        if row is None:
            raise NotFoundError("destination", dest_id)
        return row

    async def default_destination(self, media_kind: str) -> Destination:
        rows = await self.destinations(media_kind)
        if not rows:
            raise ConflictError(f"no destination for {media_kind}; add one first")
        return rows[0]

    async def folder_for(
        self, media_kind: str, path: str | None, *, field: str, actor: Actor
    ) -> str | None:
        """The library folder (destination id) for a title added with its own ``path``.

        None when ``path`` is empty or is the default folder for its kind: the title then
        follows whatever Settings says. An existing folder of that kind is reused; any other
        path becomes a new library folder (created on disk, named after it, with the
        default's naming and import mode). Refused (422 on ``field``) if it can't be used.
        """
        if not path or not path.strip():
            return None
        p = Path(await asyncio.to_thread(os.path.expanduser, path.strip()))
        if not p.is_absolute():
            raise _invalid(field, "must be a full path, like /home/you/Media/Movies")
        if why := native_folder_problem(str(p)):
            raise _invalid(field, why)
        rows = await self.destinations(media_kind)
        default = rows[0] if rows else None
        same = [d for d in rows if _same_folder(d.path, str(p))]
        if same:
            return None if default is not None and same[0].id == default.id else same[0].id
        if problem := await asyncio.to_thread(_can_write, p):
            raise _invalid(field, problem)
        async with self.db.sessionmaker() as s:
            taken = set((await s.execute(select(Destination.name))).scalars())
        name, n = p.name or str(p), 1
        while name in taken:
            n += 1
            name = f"{p.name} {n}"
        data: dict[str, Any] = {"name": name, "media_kind": media_kind, "path": str(p)}
        if default is not None:
            data |= {"naming_template_id": default.naming_template_id,
                     "link_mode": default.link_mode}  # fmt: skip
        row = await self.create_destination(data, actor=actor)
        return row.id

    # ---- CRUD -------------------------------------------------------------------------------

    def _check_fields(self, changes: dict[str, Any]) -> None:
        if "link_mode" in changes and changes["link_mode"] not in LINK_MODES:
            raise _invalid("body.link_mode", f"one of {', '.join(LINK_MODES)}")
        if "path" in changes:
            p = Path(changes["path"]).expanduser()
            if not p.is_absolute():
                raise _invalid("body.path", "must be an absolute path")
            changes["path"] = str(p)

    async def create_destination(self, data: dict[str, Any], *, actor: Actor) -> Destination:
        self._check_fields(data)
        async with self.db.sessionmaker() as s:
            node_id = data.get("node_id") or await s.scalar(
                select(StorageNode.id).where(StorageNode.kind == "local")
            )
            if await s.get(StorageNode, node_id) is None:
                raise _invalid("body.node_id", "no such storage node")
            row = Destination(**{**data, "node_id": node_id})
            if row.is_default:
                await self._clear_default(s, row.media_kind)
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                raise ConflictError(f"a destination named {data.get('name')!r} exists") from None
        await self.check(row.id)
        await self._emit("storage.destination_added", row, actor)
        return await self.destination(row.id)

    async def _clear_default(self, s: Any, media_kind: str) -> None:
        await s.execute(
            update(Destination)
            .where(Destination.media_kind == media_kind)
            .values(is_default=False)
            .execution_options(synchronize_session=False)
        )

    async def update_destination(
        self, dest_id: str, changes: dict[str, Any], *, if_match: str | None, actor: Actor
    ) -> Destination:
        self._check_fields(changes)
        async with self.db.sessionmaker() as s:
            row = await s.get(Destination, dest_id)
            if row is None:
                raise NotFoundError("destination", dest_id)
            check_if_match(row, if_match)
            if changes.get("is_default"):
                await self._clear_default(s, changes.get("media_kind", row.media_kind))
            for k, v in changes.items():
                setattr(row, k, v)
            if "path" in changes:
                row.mount_point = None  # learned again for the new folder
            try:
                await s.commit()
            except IntegrityError:
                raise ConflictError(f"a destination named {changes.get('name')!r} exists") from None
        await self.check(dest_id)
        await self._emit("storage.destination_updated", row, actor, fields=sorted(changes))
        return await self.destination(dest_id)

    async def delete_destination(self, dest_id: str, *, if_match: str | None, actor: Actor) -> None:
        async with self.db.sessionmaker() as s:
            row = await s.get(Destination, dest_id)
            if row is None:
                raise NotFoundError("destination", dest_id)
            check_if_match(row, if_match)
            await s.delete(row)
            await s.commit()  # files there stay; media_file.destination_id becomes NULL
        await self._emit("storage.destination_removed", row, actor)

    # ---- health -----------------------------------------------------------------------------

    def _prepare(self, path: Path, is_default: bool) -> None:
        """Create the zero-config default folder if missing."""
        if is_default and not path.exists():
            path.mkdir(parents=True, exist_ok=True)

    async def check(self, dest_id: str) -> Destination:
        """Probe one destination and record its status. Returns the updated row."""
        d = await self.destination(dest_id)
        path = Path(d.path)
        status, message = "online", "ok"
        learned: str | None = None
        try:
            if why := native_folder_problem(d.path):  # e.g. a Windows folder while in WSL
                raise StorageError("config", why)
            if d.mount_point and not await asyncio.to_thread(os.path.ismount, d.mount_point):
                # Never create or write anything here: it would land on the local disk
                # underneath, and vanish from view once the share is back.
                raise _Unmounted(d.mount_point)
            if d.mount_point is None:
                current = await asyncio.to_thread(mount_of, path)
                if current is not None and not _is_root(current):
                    learned = current
            await asyncio.to_thread(self._prepare, path, d.is_default)
            r = await self.local.check_access(str(path))
            if not r.exists:
                status, message = "offline", "folder does not exist"
            elif not r.is_dir or not r.writable or not r.deletable:
                status, message = "offline", r.error or "folder is not writable"
            else:
                usage = await self.local.usage(str(path))
                floor = (
                    d.free_space_min if d.free_space_min is not None else default_floor(usage.total)
                )
                if usage.free < floor:
                    status = "low_space"
                    message = f"{usage.free / GB:.1f} GB free, below the {floor / GB:.1f} GB floor"
                else:
                    message = f"{usage.free / GB:.1f} GB free"
        except _Unmounted as exc:
            status = "unmounted"
            message = (
                f"{exc.mount} isn't mounted (the NAS or drive is off or disconnected); "
                "imports wait until it's back"
            )
        except (StorageError, OSError) as exc:
            status, message = "offline", str(getattr(exc, "message", exc))
        if status != d.status:
            event = {"online": "storage.online", "low_space": "storage.low_space"}.get(
                status, "storage.offline"
            )
            await self.bus.emit(
                event,
                subject=Subject(type="destination", id=d.id),
                name=d.name,
                path=d.path,
                message=message,
            )
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(Destination)
                .where(Destination.id == d.id)
                .values(
                    status=status,
                    status_message=message,
                    checked_at=utcnow(),
                    **({"mount_point": learned} if learned else {}),
                )
                .execution_options(synchronize_session=False)
            )
            await s.commit()
        return await self.destination(d.id)

    def register_jobs(
        self, jobs: JobRunner, scheduler: Scheduler, recycle_days: Callable[[], int]
    ) -> None:
        """Daily: empty recycle-folder days older than ``recycle_days()``."""

        @jobs.handler(RECYCLE_JOB, queue="maintenance", max_attempts=1)
        async def run(ctx: JobContext) -> dict[str, Any]:
            return {"removed": await self.purge_recycle(int(recycle_days()))}

        scheduler.define(
            ScheduleDef(
                "recycle_cleanup", RECYCLE_JOB, timedelta(days=1),
                "Delete recycled files older than the retention (Settings: 7 days)",
            )
        )  # fmt: skip

    async def purge_recycle(self, days: int) -> int:
        """Delete recycle-folder days older than ``days`` in every destination."""
        cutoff = (utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        removed = 0
        for d in await self.destinations():
            for day in await asyncio.to_thread(_days, Path(d.path) / RECYCLE_DIR):
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name) and day.name < cutoff:
                    await asyncio.to_thread(shutil.rmtree, day, True)
                    removed += 1
        return removed

    async def check_all(self) -> list[Destination]:
        return [await self.check(d.id) for d in await self.destinations()]

    async def health(self) -> HealthStatus:
        rows: Sequence[Destination] = await self.check_all()
        if not rows:
            return HealthStatus(status="error", message="No destinations: add a library folder")
        bad = [
            f"{d.name}: {d.status_message}" for d in rows if d.status in ("offline", "unmounted")
        ]
        warn = [
            f"{d.name}: {d.status_message}" for d in rows if d.status in ("low_space", "degraded")
        ]
        if bad:
            return HealthStatus(status="error", message="; ".join(bad + warn))
        if warn:
            return HealthStatus(status="warning", message="; ".join(warn))
        return HealthStatus(status="ok", message=f"{len(rows)} destination(s) online")

    async def _emit(self, type_: str, d: Destination, actor: Actor, **data: Any) -> None:
        await self.bus.emit(
            type_,
            subject=Subject(type="destination", id=d.id),
            actor=actor,
            name=d.name,
            path=d.path,
            **data,
        )
