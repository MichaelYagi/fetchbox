"""Local filesystem (including OS-mounted NAS shares).

Every placement is two-phase: the new file is written (or linked) as
``<dst>.fetchbox-partial``, its size checked (and, for copies, a quick hash of the first
and last 4 MiB), then atomically renamed over ``dst``. A crash leaves only a partial file,
which ``cleanup_partials`` removes at startup. Blocking calls run in a worker thread.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel

from fetchbox.core.providers import TestResult, registry
from fetchbox.providers.storage import (
    PARTIAL_SUFFIX,
    AccessReport,
    DiskUsage,
    FileStat,
    StorageBackend,
    StorageError,
)

QUICK_HASH_SPAN = 4 * 1024 * 1024
COPY_CHUNK = 1024 * 1024
T = TypeVar("T")


class LocalSettings(BaseModel):
    pass


def quick_hash(path: str | Path) -> str:
    """sha256 of size + first and last 4 MiB: cheap identity for large media files."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(QUICK_HASH_SPAN))
        if size > QUICK_HASH_SPAN:
            f.seek(max(size - QUICK_HASH_SPAN, QUICK_HASH_SPAN))
            h.update(f.read(QUICK_HASH_SPAN))
    return h.hexdigest()


def _err(exc: OSError, what: str) -> StorageError:
    kind = {
        errno.ENOENT: "not_found",
        errno.EEXIST: "exists",
        errno.EACCES: "permission",
        errno.EPERM: "permission",
        errno.ENOSPC: "no_space",
        errno.EXDEV: "cross_device",
    }.get(exc.errno or 0, "io")
    return StorageError(kind, f"{what}: {exc.strerror or exc}")


def _partial(dst: Path) -> Path:
    return dst.with_name(dst.name + PARTIAL_SUFFIX)


def _stat(p: Path) -> FileStat:
    st = p.stat()
    return FileStat(
        path=str(p),
        is_dir=p.is_dir(),
        size=st.st_size,
        mtime=datetime.fromtimestamp(st.st_mtime, UTC),
        device=st.st_dev,
    )


def _finish(tmp: Path, dst: Path, expected_size: int) -> None:
    got = tmp.stat().st_size
    if got != expected_size:
        tmp.unlink(missing_ok=True)
        raise StorageError("verify", f"size check failed for {dst.name}: {got} != {expected_size}")
    os.replace(tmp, dst)


def _hardlink(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _partial(dst)
    tmp.unlink(missing_ok=True)
    try:
        os.link(src, tmp)
    except OSError as exc:
        raise _err(exc, f"hardlink {src.name}") from None
    _finish(tmp, dst, src.stat().st_size)


def _copy(src: Path, dst: Path, verify: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _partial(dst)
    try:
        with src.open("rb") as fi, tmp.open("wb") as fo:
            shutil.copyfileobj(fi, fo, COPY_CHUNK)
            fo.flush()
            os.fsync(fo.fileno())
        shutil.copystat(src, tmp)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise _err(exc, f"copy {src.name}") from None
    if verify and quick_hash(src) != quick_hash(tmp):
        tmp.unlink(missing_ok=True)
        raise StorageError("verify", f"copy of {src.name} does not match the original")
    _finish(tmp, dst, src.stat().st_size)


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if src.stat().st_dev == (dst.parent.stat().st_dev):
            os.replace(src, dst)  # same filesystem: an atomic rename
            return
    except OSError as exc:
        raise _err(exc, f"move {src.name}") from None
    _copy(src, dst, verify=True)
    src.unlink()


def _symlink(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _partial(dst)
    tmp.unlink(missing_ok=True)
    try:
        os.symlink(src, tmp)
    except OSError as exc:
        raise _err(exc, f"symlink {src.name}") from None
    os.replace(tmp, dst)


def _delete(p: Path, recycle_dir: str | None) -> str | None:
    if not p.exists() and not p.is_symlink():
        return None
    if recycle_dir is None:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return None
    bin_ = Path(recycle_dir) / datetime.now(UTC).strftime("%Y-%m-%d")
    bin_.mkdir(parents=True, exist_ok=True)
    target = bin_ / p.name
    if target.exists():
        target = bin_ / f"{p.stem}.{uuid.uuid4().hex[:8]}{p.suffix}"
    if p.is_file():
        _move(p, target)
    else:
        shutil.move(str(p), str(target))
    return str(target)


def _check(path: Path, link_source: Path | None) -> AccessReport:
    if not path.exists():
        return AccessReport(exists=False)
    if not path.is_dir():
        return AccessReport(exists=True, error="not a directory")
    r = AccessReport(exists=True, is_dir=True, readable=os.access(path, os.R_OK))
    probe = path / f".fetchbox-probe-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_bytes(b"fetchbox")
        r.writable = True
        if link_source is not None and link_source.exists():
            r.same_fs_as = link_source.stat().st_dev == path.stat().st_dev
            src_probe = (link_source if link_source.is_dir() else link_source.parent) / (
                f".fetchbox-probe-{uuid.uuid4().hex[:8]}"
            )
            linked = path / (probe.name + ".link")
            try:
                src_probe.write_bytes(b"fetchbox")
                os.link(src_probe, linked)
                r.hardlink_with = True
            except OSError:
                r.hardlink_with = False
            finally:
                linked.unlink(missing_ok=True)
                src_probe.unlink(missing_ok=True)
        probe.unlink()
        r.deletable = True
    except OSError as exc:
        r.error = exc.strerror or str(exc)
        probe.unlink(missing_ok=True)
    return r


def cleanup_partials(root: str | Path) -> list[str]:
    """Remove ``*.fetchbox-partial`` left by an interrupted operation (roll back)."""
    removed: list[str] = []
    base = Path(root)
    if not base.is_dir():
        return removed
    for p in base.rglob(f"*{PARTIAL_SUFFIX}"):
        try:
            p.unlink()
            removed.append(str(p))
        except OSError:
            pass
    return removed


class LocalStorage(StorageBackend):
    kind: ClassVar[str] = "local"
    Settings: ClassVar[type[BaseModel]] = LocalSettings
    capabilities: ClassVar[frozenset[str]] = frozenset({"hardlink", "symlink", "recycle"})

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        try:
            return await asyncio.to_thread(fn, *args)
        except StorageError:
            raise
        except OSError as exc:
            raise _err(exc, getattr(fn, "__name__", "file operation").lstrip("_")) from None

    async def test(self) -> TestResult:
        return TestResult(ok=True, message="local filesystem")

    async def stat(self, path: str) -> FileStat | None:
        p = Path(path)
        return await self._run(lambda: _stat(p) if p.exists() else None)

    async def list(self, path: str, recursive: bool = False) -> AsyncIterator[FileStat]:
        p = Path(path)
        entries = await self._run(lambda: list(p.rglob("*") if recursive else p.iterdir()))
        for e in entries:
            yield await self._run(_stat, e)

    async def usage(self, path: str) -> DiskUsage:
        u = await self._run(shutil.disk_usage, path)
        return DiskUsage(total=u.total, used=u.used, free=u.free)

    async def check_access(self, path: str, *, link_source: str | None = None) -> AccessReport:
        return await self._run(_check, Path(path), Path(link_source) if link_source else None)

    async def move(self, src: str, dst: str) -> None:
        await self._run(_move, Path(src), Path(dst))

    async def copy(self, src: str, dst: str, verify: bool = True) -> None:
        await self._run(_copy, Path(src), Path(dst), verify)

    async def hardlink(self, src: str, dst: str) -> None:
        await self._run(_hardlink, Path(src), Path(dst))

    async def symlink(self, src: str, dst: str) -> None:
        await self._run(_symlink, Path(src), Path(dst))

    async def delete(self, path: str, *, recycle_dir: str | None = None) -> str | None:
        return await self._run(_delete, Path(path), recycle_dir)


registry.register(LocalStorage)
