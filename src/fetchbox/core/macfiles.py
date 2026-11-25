"""macOS's ``._`` files ("AppleDouble"): on a share or disk that can't hold a Mac file's
extra attributes (a NAS over SMB, exFAT), macOS writes them to a hidden ``._<name>`` file
next to it. They aren't media: they're never imported, and Fetchbox removes them from
library folders (only beside files, or left behind by a file that's gone)."""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

PREFIX = "._"
_XATTR_NOFOLLOW = 0x0001


def is_appledouble(name: str) -> bool:
    return os.path.basename(name).startswith(PREFIX)


def _libc() -> ctypes.CDLL | None:
    if sys.platform != "darwin":
        return None
    try:
        return ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    except OSError:
        return None


def _clear_attributes(path: Path) -> None:
    """Drop a file's extra attributes on a Mac (quarantine, provenance, Finder info), so
    macOS has nothing to keep in a ``._`` file for it. Never raises."""
    libc = _libc()
    if libc is None:
        return
    raw = os.fsencode(path)
    size = libc.listxattr(raw, None, 0, _XATTR_NOFOLLOW)
    if size <= 0:
        return
    buf = ctypes.create_string_buffer(size)
    size = libc.listxattr(raw, buf, size, _XATTR_NOFOLLOW)
    for name in buf.raw[: max(size, 0)].split(b"\0"):
        if name:
            libc.removexattr(raw, name, _XATTR_NOFOLLOW)


def tidy(path: str | Path) -> None:
    """After placing a file: no ``._`` file beside it. Never raises."""
    p = Path(path)
    with contextlib.suppress(OSError):
        _clear_attributes(p)
    with contextlib.suppress(OSError):
        (p.parent / f"{PREFIX}{p.name}").unlink(missing_ok=True)


def sweep(folder: str | Path) -> list[str]:
    """Remove ``._`` files in ``folder`` and below, where the file they belong to is a file
    or is gone (one beside a folder, e.g. its custom icon, stays). Paths removed."""
    removed: list[str] = []
    root = Path(folder)
    if not root.is_dir():
        return removed
    for dirpath, _dirs, files in os.walk(root):
        names = set(files)
        for f in files:
            if not f.startswith(PREFIX) or f == PREFIX:
                continue
            owner = Path(dirpath) / f[len(PREFIX) :]
            if owner.name in names or not owner.exists():
                with contextlib.suppress(OSError):
                    (Path(dirpath) / f).unlink()
                    removed.append(str(Path(dirpath) / f))
    return removed
