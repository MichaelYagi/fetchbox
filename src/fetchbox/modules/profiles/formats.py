"""Custom formats: named sets of conditions matched against a parsed release.

Matching follows Radarr/Sonarr so imported formats behave the same:

* every condition marked ``required`` must match;
* for each condition *type* that has non-required conditions, at least one of them
  must match (conditions of one type are OR'd, different types are AND'd);
* ``negate`` inverts a single condition before the above.

A format with no conditions never matches. Scores live on the profile
(``format_scores``), falling back to the format's ``default_score``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from fetchbox.modules.releases.parser import ParsedRelease

MAX_PATTERN = 500

SOURCES = {
    "cam", "telesync", "telecine", "workprint", "screener", "regional", "sdtv", "hdtv", "dvd",
    "webrip", "webdl", "bluray",
}  # fmt: skip
MODIFIERS = {"remux", "brdisk", "rawhd"}
RESOLUTIONS = {360, 480, 576, 720, 1080, 2160}
VIDEO_CODECS = {"h264", "h265", "av1", "vc1", "mpeg2", "xvid", "divx", "vp9"}
HDR = {"DV", "HDR10+", "HDR10", "HLG"}
AUDIO_CODECS = {
    "truehd", "dts-x", "dts-hd ma", "dts-hd hra", "dts-hd", "dts-es", "dts", "eac3", "ac3",
    "aac", "flac", "opus", "mp3", "pcm",
}  # fmt: skip
CHANNELS = {"1.0", "2.0", "2.1", "5.1", "6.1", "7.1"}
FLAGS = {
    "proper", "repack", "real", "internal", "limited", "hybrid", "three_d", "hardcoded_subs",
    "multi_language", "dubbed", "atmos", "sample", "complete_series", "full_season",
}  # fmt: skip
INDEXER_FLAGS = {"freeleech", "halfleech", "doubleupload", "internal", "scene", "nuked"}

_ALLOWED: dict[str, set[str] | set[int] | None] = {
    "source": SOURCES,
    "modifier": MODIFIERS,
    "resolution": RESOLUTIONS,
    "video_codec": VIDEO_CODECS,
    "hdr": HDR,
    "audio_codec": AUDIO_CODECS,
    "audio_channels": CHANNELS,
    "flag": FLAGS,
    "indexer_flag": INDEXER_FLAGS,
    "language": None,  # ISO 639-1, checked by shape
    "subtitle_language": None,
    "streaming_service": None,  # open set (AMZN, NF, ...)
}


class _Base(BaseModel):
    name: str | None = Field(default=None, max_length=100, description="Label shown in the UI")
    negate: bool = False
    required: bool = False


class PatternCondition(_Base):
    """Case-insensitive regular expression search."""

    type: Literal["release_title", "release_group", "edition"]
    pattern: str = Field(min_length=1, max_length=MAX_PATTERN)

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            re.compile(v, re.I)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from None
        return v


class ValueCondition(_Base):
    """Matches when the release has any of ``values`` for that attribute."""

    type: Literal[
        "source",
        "modifier",
        "resolution",
        "video_codec",
        "hdr",
        "audio_codec",
        "audio_channels",
        "language",
        "subtitle_language",
        "streaming_service",
        "flag",
        "indexer_flag",
    ]
    values: list[str | int] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _known_values(self) -> ValueCondition:
        allowed = _ALLOWED[self.type]
        if self.type == "resolution":
            bad = [v for v in self.values if not isinstance(v, int) or v not in RESOLUTIONS]
        elif allowed is not None:
            bad = [v for v in self.values if v not in allowed]
        elif self.type in ("language", "subtitle_language"):
            bad = [
                v for v in self.values if not (isinstance(v, str) and re.fullmatch("[a-z]{2}", v))
            ]
        else:
            bad = [v for v in self.values if not (isinstance(v, str) and 0 < len(v) <= 20)]
        if bad:
            hint = f"; allowed: {', '.join(map(str, sorted(allowed)))}" if allowed else ""
            raise ValueError(f"unknown {self.type} value(s) {bad}{hint}")
        return self


class SizeCondition(_Base):
    """Release size in MB (inclusive bounds); never matches when the size is unknown."""

    type: Literal["size"]
    min_mb: float | None = Field(default=None, ge=0)
    max_mb: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _bounds(self) -> SizeCondition:
        if self.min_mb is None and self.max_mb is None:
            raise ValueError("set min_mb, max_mb or both")
        if self.min_mb is not None and self.max_mb is not None and self.max_mb < self.min_mb:
            raise ValueError("max_mb must be >= min_mb")
        return self


Condition = Annotated[
    PatternCondition | ValueCondition | SizeCondition, Field(discriminator="type")
]


@dataclass(frozen=True)
class ReleaseInfo:
    """What a format is matched against: the parse plus indexer-side facts."""

    parsed: ParsedRelease
    size: int | None = None  # bytes
    indexer_flags: frozenset[str] = field(default_factory=frozenset)


@lru_cache(maxsize=1024)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


def _flags(p: ParsedRelease) -> set[str]:
    on = {
        "proper": p.proper,
        "repack": p.repack,
        "real": p.revision.real > 0,
        "internal": p.internal,
        "limited": p.limited,
        "hybrid": p.hybrid,
        "three_d": p.three_d,
        "hardcoded_subs": p.hardcoded_subs,
        "multi_language": p.multi_language,
        "dubbed": p.dubbed,
        "atmos": p.atmos,
        "sample": p.sample,
        "complete_series": p.complete_series,
        "full_season": p.full_season,
    }
    return {k for k, v in on.items() if v}


def _attribute(info: ReleaseInfo, kind: str) -> set[str | int]:
    p = info.parsed
    match kind:
        case "source":
            return {p.source} if p.source else set()
        case "modifier":
            return {p.modifier} if p.modifier else set()
        case "resolution":
            return {p.resolution} if p.resolution else set()
        case "video_codec":
            return {p.video_codec} if p.video_codec else set()
        case "hdr":
            return set(p.hdr)
        case "audio_codec":
            return set(p.audio_codecs)
        case "audio_channels":
            return {p.audio_channels} if p.audio_channels else set()
        case "language":
            return set(p.languages)
        case "subtitle_language":
            return set(p.subtitle_languages)
        case "streaming_service":
            return {p.streaming_service} if p.streaming_service else set()
        case "flag":
            return set(_flags(p))
        case "indexer_flag":
            return set(info.indexer_flags)
    raise ValueError(kind)  # pragma: no cover


def condition_matches(
    c: PatternCondition | ValueCondition | SizeCondition, info: ReleaseInfo
) -> bool:
    if isinstance(c, PatternCondition):
        text = {
            "release_title": info.parsed.raw,
            "release_group": info.parsed.group or "",
            "edition": info.parsed.edition or "",
        }[c.type]
        hit = bool(text) and _compiled(c.pattern).search(text) is not None
    elif isinstance(c, SizeCondition):
        if info.size is None:
            return False  # unknown size never matches, negated or not
        mb = info.size / 1_000_000
        hit = (c.min_mb is None or mb >= c.min_mb) and (c.max_mb is None or mb <= c.max_mb)
    else:
        hit = not _attribute(info, c.type).isdisjoint(c.values)
    return hit != c.negate


def format_matches(
    conditions: Iterable[PatternCondition | ValueCondition | SizeCondition], info: ReleaseInfo
) -> bool:
    by_type: dict[str, list[bool]] = defaultdict(list)
    any_condition = False
    for c in conditions:
        any_condition = True
        hit = condition_matches(c, info)
        if c.required:
            if not hit:
                return False
        else:
            by_type[c.type].append(hit)
    return any_condition and all(any(hits) for hits in by_type.values())


@dataclass(frozen=True)
class FormatSpec:
    """A custom format as the scorer sees it (decoupled from the ORM row)."""

    id: str
    name: str
    conditions: tuple[PatternCondition | ValueCondition | SizeCondition, ...]
    default_score: int = 0


@dataclass(frozen=True)
class MatchedFormat:
    id: str
    name: str
    score: int


@dataclass(frozen=True)
class FormatScore:
    total: int
    matched: tuple[MatchedFormat, ...]


def score_release(
    info: ReleaseInfo, formats: Iterable[FormatSpec], scores: Mapping[str, int]
) -> FormatScore:
    matched = tuple(
        MatchedFormat(f.id, f.name, scores.get(f.id, f.default_score))
        for f in formats
        if format_matches(f.conditions, info)
    )
    return FormatScore(sum(m.score for m in matched), matched)
