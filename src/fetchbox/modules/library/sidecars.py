"""Sidecar files: files that belong to a media file (subtitles, lyrics, artwork), added by
plugins. A sidecar sits beside its owner as ``<owner's name without extension>.<suffix>``:
"Dune (2021).mkv" + "en.srt" = "Dune (2021).en.srt". Its path is never stored, only its
suffix, so wherever the owner is renamed or moved, the sidecar's place follows by the same
rule; Fetchbox moves, recycles and deletes the sidecar files along with their owner."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from fetchbox.modules.library.music_files import AUDIO_EXTS

# one or more dot-separated parts, the last an extension: "srt", "en.srt", "forced.en.srt"
_SUFFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}(?:\.[A-Za-z0-9][A-Za-z0-9_-]{0,31}){0,3}$")


class SidecarError(ValueError):
    pass


def check_suffix(suffix: str) -> str:
    """A suffix a sidecar may have: never a path, and never a video or audio extension
    (Fetchbox would take the sidecar for media)."""
    if len(suffix) > 64 or not _SUFFIX.match(suffix):
        raise SidecarError(
            f"not a sidecar suffix: {suffix!r} (letters, digits, - and _, parts joined by "
            "dots, like 'en.srt')"
        )
    from fetchbox.modules.library.service import VIDEO_EXTS  # (service imports this module)

    ext = suffix.rsplit(".", 1)[-1].lower()
    if ext in VIDEO_EXTS or f".{ext}" in AUDIO_EXTS:
        raise SidecarError(f"a sidecar can't end in .{ext}: Fetchbox would take it for media")
    return suffix


def sidecar_rel(owner_rel: str, suffix: str) -> str:
    """The sidecar's path, relative like its owner's."""
    p = PurePosixPath(owner_rel)
    return str(p.with_name(f"{p.stem}.{suffix}"))


def write_atomically(path: Path, data: bytes) -> None:
    """Write to a temporary name beside ``path``, then replace: never a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.fetchbox-partial")
    tmp.write_bytes(data)
    os.replace(tmp, path)
