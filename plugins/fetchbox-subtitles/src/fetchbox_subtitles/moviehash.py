"""OpenSubtitles' file hash: the size plus the sum of the first and last 64 KB read as
64-bit little-endian numbers. It matches subtitles timed to this exact release."""

from __future__ import annotations

import struct
from pathlib import Path

CHUNK = 64 * 1024


def moviehash(path: Path) -> str | None:
    """The hash as 16 hex digits; None for a file too small (or unreadable)."""
    try:
        size = path.stat().st_size
        if size < 2 * CHUNK:
            return None
        total = size
        with path.open("rb") as f:
            for offset in (0, size - CHUNK):
                f.seek(offset)
                for (word,) in struct.iter_unpack("<Q", f.read(CHUNK)):
                    total = (total + word) & 0xFFFFFFFFFFFFFFFF
        return f"{total:016x}"
    except OSError:
        return None
