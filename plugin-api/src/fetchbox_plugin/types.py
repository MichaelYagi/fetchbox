"""What ``ctx`` hands a plugin: plain read-only values, never Fetchbox's database rows, so
a plugin can't change the library behind Fetchbox's back and internal columns can change
without breaking it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TitleInfo:
    id: str
    kind: str  # "movie", "series", "music" or a plugin's kind
    name: str
    year: int | None
    ids: dict[str, Any] = field(default_factory=dict)  # tmdb, tvdb, imdb, mbid: those it has
    monitored: bool = True
    folder: str | None = None  # its own folder in the library folder, once decided


@dataclass(frozen=True)
class ItemInfo:
    """What's wanted and downloaded: a movie, an episode, an album."""

    id: str
    title_id: str
    level: str  # "movie", "episode" or "album"
    name: str | None  # the episode's or album's name (a movie's is its title's)
    season: int | None  # an episode's season
    number: int | None  # an episode's number in its season
    released: date | None  # air date, release date
    monitored: bool  # wanted: searched for and downloaded
    state: str  # missing, searching, grabbed, downloading, importing, acquired
    ids: dict[str, Any] = field(default_factory=dict)  # an album's mbid


@dataclass(frozen=True)
class FileInfo:
    id: str
    title_id: str | None
    path: Path  # where it is now
    size: int
    quality: str  # "WEBDL-1080p", "FLAC"
    item_ids: tuple[str, ...]  # the wanted items (episode, movie, album) it is
    imported_at: datetime


@dataclass(frozen=True)
class SidecarInfo:
    file_id: str
    suffix: str  # "en.srt"
    path: Path
    plugin: str  # the plugin that added it
    meta: dict[str, Any] = field(default_factory=dict)
