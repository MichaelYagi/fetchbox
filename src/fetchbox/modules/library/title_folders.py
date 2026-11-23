"""A title's own folder: which folders already on disk belong to a title being added.

The add dialog lists them ("Use existing: The Bear, 3 seasons, 24 episodes") next to the
folder the naming would create, so new files join a show, movie or artist already there
instead of going to a second folder with the naming's name.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

from fetchbox.modules.library.existing import folder_hints
from fetchbox.modules.library.music_files import is_audio
from fetchbox.modules.library.naming import season_of_folder
from fetchbox.modules.library.service import VIDEO_EXTS
from fetchbox.modules.releases.decision import title_key
from fetchbox.modules.storage.service import RECYCLE_DIR

MAX_FILES = 5000  # counting stops here: a huge folder isn't walked to the end


def _counts(folder: Path, kind: str) -> tuple[int, int]:
    """(media files, season folders) in a folder."""
    files = 0
    for _dir, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            ext = os.path.splitext(f)[1].lower().lstrip(".")
            if (
                is_audio(f) if kind == "music" else ext in VIDEO_EXTS
            ) and "sample" not in f.lower():
                files += 1
        if files >= MAX_FILES:
            break
    seasons = 0
    if kind == "series":
        with contextlib.suppress(OSError):
            seasons = sum(
                1 for e in folder.iterdir() if e.is_dir() and season_of_folder(e.name) is not None
            )
    return files, seasons


def folder_choices(
    root: Path,
    kind: str,
    *,
    name: str,
    year: int | None = None,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
) -> list[dict[str, Any]]:
    """Folders in ``root`` that belong to this title: one with its TMDB/TVDB id in the
    name, or its title (and year, when both say one). Best match first."""
    try:
        entries = [
            e
            for e in root.iterdir()
            if e.is_dir() and not e.name.startswith(".") and e.name != RECYCLE_DIR
        ]
    except OSError:
        return []
    want = title_key(name)
    out: list[dict[str, Any]] = []
    for e in entries:
        if kind == "music":
            match = "title" if title_key(e.name) == want else None
        else:
            h = folder_hints(e.name)
            if (tmdb_id and h.get("tmdb_id") == tmdb_id) or (
                tvdb_id and h.get("tvdb_id") == tvdb_id
            ):
                match = "id"
            elif (
                title_key(str(h.get("title") or "")) == want
                and not (h.get("tmdb_id") or h.get("tvdb_id"))  # another title's id: not it
                and (year is None or h.get("year") in (None, year))
            ):
                match = "title"
            else:
                match = None
        if match is None:
            continue
        files, seasons = _counts(e, kind)
        out.append({"name": e.name, "match": match, "files": files, "seasons": seasons})
    out.sort(key=lambda c: (c["match"] != "id", -c["files"], c["name"].lower()))
    return out
