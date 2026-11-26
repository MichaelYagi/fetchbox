"""Media kinds: the kinds of thing Fetchbox fetches, and what it needs to know about each.

Movies, series and music are the core kinds. They are built in, always present and never
replaced. A plugin may add a kind of its own (comics, audiobooks) with ``register``; it gets
the same treatment from the code that reads this list: a download category, a default
library folder, the file types that count as its media.

Code that handles every kind the same way reads the kind's descriptor here instead of
comparing against "movie", "series" or "music". Code that is about one core kind (TMDB
lookups for movies and series, MusicBrainz for music) keeps comparing directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# File types, without the dot, lowercase
VIDEO_EXTS = frozenset(
    {"mkv", "mp4", "m4v", "avi", "mov", "wmv", "ts", "m2ts", "webm", "mpg", "mpeg"}
)
AUDIO_EXTS = frozenset(
    {"flac", "mp3", "m4a", "mp4", "aac", "ogg", "opus", "wav", "aiff", "aif", "wma", "ape", "wv"}
)

_KEY = re.compile(r"^[a-z][a-z0-9_]{1,9}$")  # fits the 10-character kind columns


@dataclass(frozen=True)
class MediaKind:
    key: str  # stored on titles and library folders: "movie"
    label: str  # one: "Movie"
    plural: str  # many, and the default library folder's name: "Movies"
    levels: tuple[str, ...]  # title down to what has the files: ("series", "season", "episode")
    file_exts: frozenset[str]  # what counts as this kind's media file
    category: str  # the download client category: "fetchbox-movies"
    profile_media: str  # which quality profiles fit: "video" or "music"
    plugin: str | None = None  # the plugin that added it; None for a core kind

    @property
    def core(self) -> bool:
        return self.plugin is None

    def is_media_file(self, name: str) -> bool:
        """This kind's media file, by its name (a macOS ._ file beside one is not)."""
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        if base.startswith("._") or "." not in base.lstrip("."):
            return False
        return base.rsplit(".", 1)[1].lower() in self.file_exts


CORE: tuple[MediaKind, ...] = (
    MediaKind(
        key="movie",
        label="Movie",
        plural="Movies",
        levels=("movie",),
        file_exts=VIDEO_EXTS,
        category="fetchbox-movies",
        profile_media="video",
    ),
    MediaKind(
        key="series",
        label="Series",
        plural="TV",
        levels=("series", "season", "episode"),
        file_exts=VIDEO_EXTS,
        category="fetchbox-tv",
        profile_media="video",
    ),
    MediaKind(
        key="music",
        label="Artist",
        plural="Music",
        levels=("artist", "album", "track"),
        file_exts=AUDIO_EXTS,
        category="fetchbox-music",
        profile_media="music",
    ),
)
CORE_KEYS: tuple[str, ...] = tuple(k.key for k in CORE)

_kinds: dict[str, MediaKind] = {k.key: k for k in CORE}


class KindError(ValueError):
    pass


def register(kind: MediaKind) -> None:
    """Add a plugin's kind. A core kind, or another plugin's, can't be replaced."""
    if kind.plugin is None:
        raise KindError(f"{kind.key}: a plugin's kind names its plugin")
    if not _KEY.match(kind.key):
        raise KindError(f"{kind.key!r}: a kind is 2 to 10 lowercase letters, digits or _")
    if kind.key in _kinds:
        raise KindError(f"{kind.key}: already a kind ({_kinds[kind.key].plugin or 'core'})")
    if not kind.levels or not kind.file_exts:
        raise KindError(f"{kind.key}: a kind needs its levels and file types")
    if kind.category in {k.category for k in _kinds.values()}:
        raise KindError(f"{kind.key}: download category {kind.category} is taken")
    _kinds[kind.key] = kind


def unregister(plugin: str) -> None:
    """Remove every kind a plugin added (when it's turned off)."""
    for key in [k.key for k in _kinds.values() if k.plugin == plugin]:
        del _kinds[key]


def get(key: str) -> MediaKind:
    try:
        return _kinds[key]
    except KeyError:
        raise KindError(f"{key}: not a media kind") from None


def find(key: str) -> MediaKind | None:
    return _kinds.get(key)


def all_kinds() -> tuple[MediaKind, ...]:
    """Core kinds first, in their order, then plugins' kinds in the order they came."""
    return tuple(_kinds.values())


def keys() -> tuple[str, ...]:
    return tuple(_kinds)
