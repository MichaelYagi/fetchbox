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
import contextlib
import os
import re
import shutil
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select, update
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


# where drives and shares are mounted (macOS, WSL / Linux)
MOUNT_ROOTS = {"/Volumes", "/mnt", "/media", "/run/media"}


def test_folder(path: str) -> dict[str, Any]:
    """Can ``path`` be a library folder? Read-only, apart from a probe file written and
    deleted in it: a folder that doesn't exist yet is not created (saving does that)."""
    out: dict[str, Any] = {"ok": False, "exists": False, "network": False, "free": None}
    raw = path.strip()
    if not raw:
        return out | {"message": "Enter a folder"}
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        return out | {"message": "Must be a full path, like /home/you/Media/Movies"}
    if why := native_folder_problem(str(p)):
        return out | {"message": why[:1].upper() + why[1:]}
    mount = mount_of(p)
    if mount is not None and not _is_root(mount):
        import psutil

        with contextlib.suppress(Exception):
            for part in psutil.disk_partitions(all=True):
                if os.path.normcase(part.mountpoint) == os.path.normcase(mount):
                    out["network"] = part.fstype in NETWORK_FS or part.device.startswith(
                        ("//", "\\\\")
                    )
    where = " on a network share" if out["network"] else ""
    if p.exists():
        if not p.is_dir():
            return out | {"message": f"{p} is a file, not a folder"}
        out["exists"] = True
        probe = p / ".fetchbox-write-test"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            return out | {"message": f"Fetchbox can't write to {p}: {exc.strerror or exc}"}
        target = p
        what = f"Folder found{where}, Fetchbox can write to it"
    else:
        target = p.parent
        while not target.exists() and target != target.parent:
            target = target.parent
        if str(target) in MOUNT_ROOTS:
            # /Volumes/Public missing: the share (or drive) isn't connected. Never create
            # it: it would land on the local disk, under where the share goes.
            gone = target / p.relative_to(target).parts[0]
            return out | {
                "message": f"Nothing is connected at {gone}: connect the share or drive first, "
                "then test again"
            }
        if not target.is_dir() or not os.access(target, os.W_OK | os.X_OK):
            return out | {"message": f"{p} doesn't exist, and Fetchbox can't create it in {target}"}
        what = f"{p.name} doesn't exist yet{where}: Fetchbox will create it when you save"
    with contextlib.suppress(OSError):
        out["free"] = shutil.disk_usage(target).free
    if out["free"] is not None:
        what += f" ({out['free'] / GB:.1f} GB free)"
    return out | {"ok": True, "message": what}


def _norm_folder(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expanduser(path)))


def _same_folder(a: str, b: str) -> bool:
    """The same folder, however it's written: on a Mac /Volumes/Macintosh HD/X is /X,
    a symlink is where it points, and the case of a name may not matter (this touches
    the disk: call it off the event loop)."""
    if _norm_folder(a) == _norm_folder(b):
        return True
    try:
        return os.path.samefile(os.path.expanduser(a), os.path.expanduser(b))
    except (OSError, ValueError):
        return False


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

    async def merge_duplicates(self) -> list[str]:
        """Library folders of one kind that are the same folder written two ways (an older
        Fetchbox made "Movies 2" for a title added to /Volumes/Macintosh HD/.../Movies):
        their titles and files move to the first, and the others go. Names of the ones
        removed."""
        rows = sorted(
            await self.destinations(),
            key=lambda d: (not d.is_default, d.created_at, d.name),
        )
        gone: dict[str, Destination] = {}  # duplicate id -> the one it joins
        for i, d in enumerate(rows):
            if d.id in gone:
                continue
            for other in rows[i + 1 :]:
                if (
                    other.id not in gone
                    and other.media_kind == d.media_kind
                    and await asyncio.to_thread(_same_folder, d.path, other.path)
                ):
                    gone[other.id] = d
        if not gone:
            return []
        names = []
        async with self.db.sessionmaker() as s:
            for dup_id, keep in gone.items():
                await self._join(s, dup_id, keep, keep.is_default)
                dup = await s.get(Destination, dup_id)
                if dup is not None:
                    names.append(dup.name)
                    await s.delete(dup)
            await s.commit()
        log.info("storage.duplicates_merged", removed=names)
        return names

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
        same = [d for d in rows if await asyncio.to_thread(_same_folder, d.path, str(p))]
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

    async def _same_as(
        self, path: str, media_kind: str, but: str | None = None
    ) -> Destination | None:
        """The library folder of this kind that ``path`` already is, however it's written."""
        for d in await self.destinations(media_kind):
            if d.id != but and await asyncio.to_thread(_same_folder, d.path, path):
                return d
        return None

    async def _already(self, path: str, media_kind: str) -> None:
        """Refused (422 on body.path) when ``path`` is already a library folder."""
        if d := await self._same_as(path, media_kind):
            raise _invalid("body.path", f"that folder is already the library folder {d.name}")

    async def _join(self, s: Any, dup_id: str, keep: Destination, default: bool) -> None:
        """``dup_id``'s titles and files belong to ``keep`` (the same folder) from now on."""
        from fetchbox.modules.library.models import MediaFile
        from fetchbox.modules.media.models import Title

        # a title in the default folder has none of its own (it follows Settings)
        own = None if default else keep.id
        for col, dest_id in ((Title.destination_id, own), (MediaFile.destination_id, keep.id)):
            await s.execute(
                update(col.class_)
                .where(col == dup_id)
                .values(destination_id=dest_id)
                .execution_options(synchronize_session=False)
            )

    async def _titles_in(self, s: Any, dest_id: str) -> set[str]:
        """Titles with files in this library folder."""
        from fetchbox.modules.library.models import MediaFile, MediaFileItem
        from fetchbox.modules.media.models import Album, Track, WantedItem

        via_items = (
            select(WantedItem.title_id)
            .join(MediaFileItem, MediaFileItem.wanted_item_id == WantedItem.id)
            .join(MediaFile, MediaFile.id == MediaFileItem.media_file_id)
            .where(MediaFile.destination_id == dest_id)
        )
        via_tracks = (
            select(Album.title_id)
            .join(Track, Track.album_id == Album.id)
            .join(MediaFile, MediaFile.track_id == Track.id)
            .where(MediaFile.destination_id == dest_id)
        )
        return set((await s.execute(via_items.union(via_tracks))).scalars())

    async def create_destination(self, data: dict[str, Any], *, actor: Actor) -> Destination:
        self._check_fields(data)
        await self._already(data.get("path", ""), data.get("media_kind", ""))
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
        before = await self.destination(dest_id)
        moved = "path" in changes and not await asyncio.to_thread(
            _same_folder, before.path, changes["path"]
        )
        kind = changes.get("media_kind", before.media_kind)
        # moved onto another library folder (e.g. back to an old one): the two become one
        other = await self._same_as(changes["path"], kind, dest_id) if "path" in changes else None
        kept: Destination | None = None
        async with self.db.sessionmaker() as s:
            row = await s.get(Destination, dest_id)
            if row is None:
                raise NotFoundError("destination", dest_id)
            check_if_match(row, if_match)
            if moved:
                kept = await self._keep_old_folder(s, row)
            if other is not None:
                await self._join(s, other.id, row, bool(changes.get("is_default", row.is_default)))
                gone = await s.get(Destination, other.id)
                if gone is not None:
                    await s.delete(gone)
                    await s.flush()
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
        if kept is not None:
            await self.check(kept.id)
            await self._emit("storage.destination_added", kept, actor)
        return await self.destination(dest_id)

    async def _keep_old_folder(self, s: Any, row: Destination) -> Destination | None:
        """Before ``row`` moves to another folder: the files already in its folder (and
        their titles) get a library folder of their own there, so nothing goes missing
        and those titles keep their upgrades where their files are. New titles go to the
        new folder. None when the folder holds nothing Fetchbox knows of."""
        from fetchbox.modules.library.models import MediaFile
        from fetchbox.modules.media.models import Title

        if not await s.scalar(
            select(MediaFile.id).where(MediaFile.destination_id == row.id).limit(1)
        ):
            return None
        titles = await self._titles_in(s, row.id)
        taken = set((await s.execute(select(Destination.name))).scalars())
        name, n = f"{row.name} (old folder)", 1
        while name in taken:
            n += 1
            name = f"{row.name} (old folder {n})"
        old = Destination(
            node_id=row.node_id, name=name, path=row.path, media_kind=row.media_kind,
            naming_template_id=row.naming_template_id, link_mode=row.link_mode,
            free_space_min=row.free_space_min, recycle=row.recycle, is_default=False,
            mount_point=row.mount_point,
        )  # fmt: skip
        s.add(old)
        await s.flush()
        await s.execute(
            update(MediaFile)
            .where(MediaFile.destination_id == row.id)
            .values(destination_id=old.id)
            .execution_options(synchronize_session=False)
        )
        if titles:
            await s.execute(
                update(Title)
                .where(Title.id.in_(titles))
                .values(destination_id=old.id)
                .execution_options(synchronize_session=False)
            )
        return old

    async def delete_destination(self, dest_id: str, *, if_match: str | None, actor: Actor) -> None:
        async with self.db.sessionmaker() as s:
            row = await s.get(Destination, dest_id)
            if row is None:
                raise NotFoundError("destination", dest_id)
            check_if_match(row, if_match)
            from fetchbox.modules.library.models import MediaFile
            from fetchbox.modules.media.models import Title

            titles = await self._titles_in(s, dest_id) | set(
                (await s.execute(select(Title.id).where(Title.destination_id == dest_id))).scalars()
            )
            files = await s.scalar(
                select(func.count())
                .select_from(MediaFile)
                .where(MediaFile.destination_id == dest_id)
            )
            if titles or files:
                n = len(titles)
                raise ConflictError(
                    f"{row.name} still holds {n} title{'' if n == 1 else 's'} ({files} "
                    f"file{'' if files == 1 else 's'}): move them to another library folder "
                    "first (each title's page), so Fetchbox doesn't lose track of them"
                )
            await s.delete(row)
            await s.commit()  # nothing of Fetchbox's was in it
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
