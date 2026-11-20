"""The quality catalog: every quality name the parser can produce, in base order.

Names are what ``releases.parser.quality_name`` returns. Profiles arrange these into a
ladder of tiers; the catalog only provides the names, a stable base order (used for
display and for sorting within a tier) and default size bounds.

Size bounds are MB per minute of runtime, like Radarr/Sonarr "quality definitions":
a 2-hour movie at 1080p WEB between 4 and 137 MB/min is 0.5-16 GB. ``None`` means
unbounded. Profiles may override them per quality.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field, model_validator


@dataclass(frozen=True)
class Quality:
    name: str
    resolution: int | None
    min_mb_per_min: float
    max_mb_per_min: float | None


def _q(name: str, res: int | None, lo: float, hi: float | None) -> Quality:
    return Quality(name, res, lo, hi)


CATALOG: tuple[Quality, ...] = (
    _q("Unknown", None, 0, None),
    _q("WORKPRINT", None, 0, 100),
    _q("CAM", None, 0, 100),
    _q("TELESYNC", None, 0, 100),
    _q("TELECINE", None, 0, 100),
    _q("REGIONAL", 480, 0, 100),
    _q("DVDSCR", 480, 0, 100),
    _q("SDTV", 480, 2, 100),
    _q("DVD", 480, 2, 100),
    _q("Unknown-480p", 480, 2, 100),
    _q("WEBRip-480p", 480, 2, 100),
    _q("WEBDL-480p", 480, 2, 100),
    _q("Bluray-480p", 480, 2, 100),
    _q("Bluray-576p", 576, 3, 100),
    _q("DVD-Remux", 480, 2, None),
    _q("Unknown-720p", 720, 3, 137),
    _q("HDTV-720p", 720, 3, 125),
    _q("WEBRip-720p", 720, 3, 137),
    _q("WEBDL-720p", 720, 3, 137),
    _q("Bluray-720p", 720, 4, 137),
    _q("Raw-HD", 1080, 4, None),
    _q("Unknown-1080p", 1080, 4, 137),
    _q("HDTV-1080p", 1080, 4, 137),
    _q("WEBRip-1080p", 1080, 4, 137),
    _q("WEBDL-1080p", 1080, 4, 137),
    _q("Bluray-1080p", 1080, 4, 155),
    _q("Remux-1080p", 1080, 17, 400),
    _q("Unknown-2160p", 2160, 15, 400),
    _q("HDTV-2160p", 2160, 15, 400),
    _q("WEBRip-2160p", 2160, 15, 400),
    _q("WEBDL-2160p", 2160, 15, 400),
    _q("Bluray-2160p", 2160, 35, 400),
    _q("Remux-2160p", 2160, 35, None),
    _q("BR-DISK", None, 20, None),
)
BY_NAME: dict[str, Quality] = {q.name: q for q in CATALOG}
RANK: dict[str, int] = {q.name: i for i, q in enumerate(CATALOG)}


class SizeLimit(BaseModel):
    """Per-quality override, MB per minute of runtime."""

    min: float = Field(ge=0)
    preferred: float | None = Field(default=None, ge=0)
    max: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> SizeLimit:
        if self.max is not None and self.max < self.min:
            raise ValueError("max must be >= min")
        if self.preferred is not None and not (
            self.min <= self.preferred <= (self.max if self.max is not None else self.preferred)
        ):
            raise ValueError("preferred must be between min and max")
        return self


def default_size(name: str) -> SizeLimit:
    q = BY_NAME[name]
    return SizeLimit(min=q.min_mb_per_min, max=q.max_mb_per_min)


def size_ok(limit: SizeLimit, size_bytes: int, runtime_minutes: int) -> bool:
    if runtime_minutes <= 0:
        return True  # unknown runtime: don't reject on size
    mb_per_min = size_bytes / 1_000_000 / runtime_minutes
    return mb_per_min >= limit.min and (limit.max is None or mb_per_min <= limit.max)
