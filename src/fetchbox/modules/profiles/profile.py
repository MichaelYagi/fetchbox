"""Quality profile semantics: ladder, cutoff, format scores, languages.

A ladder is an ordered list of tiers, worst first. Qualities in the same tier rank
equal (e.g. "WEB 1080p" = WEBDL-1080p + WEBRip-1080p). Qualities not in the ladder
are not allowed. The cutoff is a tier: once a file reaches it (and the cutoff format
score, if set), automatic upgrades stop.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel, Field, model_validator

from fetchbox.modules.profiles.formats import FormatScore, FormatSpec, ReleaseInfo, score_release
from fetchbox.modules.profiles.qualities import BY_NAME, SizeLimit, default_size
from fetchbox.modules.releases.parser import ParsedRelease

ORIGINAL = "original"  # pseudo-language: the title's original language


class Tier(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    qualities: list[str] = Field(min_length=1)


class ProfileSpec(BaseModel):
    """The editable content of a profile, validated as a whole."""

    name: str = Field(min_length=1, max_length=100)
    ladder: list[Tier] = Field(min_length=1, description="Tiers, worst first")
    cutoff: str = Field(description="Name of the tier at which upgrades stop")
    upgrade_allowed: bool = True
    min_format_score: int = Field(default=0, description="Releases scoring below are rejected")
    cutoff_format_score: int | None = Field(
        default=None, description="Upgrades also continue until this score is reached"
    )
    format_scores: dict[str, int] = Field(
        default_factory=dict, description="custom_format_id -> score (overrides default_score)"
    )
    languages: list[str] = Field(
        default_factory=lambda: [ORIGINAL],
        description='Allowed audio languages (ISO 639-1 or "original"); empty = any',
    )
    sizes: dict[str, SizeLimit] = Field(
        default_factory=dict, description="Per-quality size overrides, MB per minute"
    )

    @model_validator(mode="after")
    def _consistent(self) -> ProfileSpec:
        names = [t.name for t in self.ladder]
        if len(set(names)) != len(names):
            raise ValueError("tier names must be unique")
        seen: set[str] = set()
        for tier in self.ladder:
            for q in tier.qualities:
                if q not in BY_NAME:
                    raise ValueError(f"unknown quality {q!r} (see GET /qualities)")
                if q in seen:
                    raise ValueError(f"quality {q!r} appears in more than one tier")
                seen.add(q)
        if self.cutoff not in names:
            raise ValueError(f"cutoff {self.cutoff!r} is not a tier in the ladder")
        for lang in self.languages:
            if lang != ORIGINAL and not re.fullmatch("[a-z]{2}", lang):
                raise ValueError(f'language {lang!r} must be ISO 639-1 or "original"')
        for q in self.sizes:
            if q not in BY_NAME:
                raise ValueError(f"sizes: unknown quality {q!r}")
        return self

    def tier_index(self, quality: str) -> int | None:
        for i, tier in enumerate(self.ladder):
            if quality in tier.qualities:
                return i
        return None

    @property
    def cutoff_index(self) -> int:
        return next(i for i, t in enumerate(self.ladder) if t.name == self.cutoff)

    def size_limit(self, quality: str) -> SizeLimit:
        return self.sizes.get(quality) or default_size(quality)


@dataclass(frozen=True)
class Evaluation:
    quality: str
    tier: str | None
    tier_index: int | None  # higher is better; None = not allowed
    format_score: FormatScore
    meets_min_score: bool
    cutoff_met: bool

    @property
    def allowed(self) -> bool:
        return self.tier_index is not None


def evaluate(profile: ProfileSpec, info: ReleaseInfo, formats: Iterable[FormatSpec]) -> Evaluation:
    quality = info.parsed.quality
    idx = profile.tier_index(quality)
    score = score_release(info, formats, profile.format_scores)
    cutoff_met = (
        idx is not None
        and idx >= profile.cutoff_index
        and (profile.cutoff_format_score is None or score.total >= profile.cutoff_format_score)
    )
    return Evaluation(
        quality=quality,
        tier=profile.ladder[idx].name if idx is not None else None,
        tier_index=idx,
        format_score=score,
        meets_min_score=score.total >= profile.min_format_score,
        cutoff_met=cutoff_met,
    )


def language_ok(profile: ProfileSpec, parsed: ParsedRelease, original_language: str | None) -> bool:
    """Whether the release's audio fits the profile.

    A release that states no language is assumed to be in the original language. A
    multi-language release is assumed to include the original track.
    """
    if not profile.languages:
        return True
    allowed = {lang for lang in profile.languages if lang != ORIGINAL}
    wants_original = ORIGINAL in profile.languages
    if wants_original and original_language:
        allowed.add(original_language)
    stated = set(parsed.languages)
    if not stated:
        # Unknown original language: nothing to contradict the profile.
        return wants_original or original_language is None or original_language in allowed
    if wants_original and parsed.multi_language:
        return True
    return not stated.isdisjoint(allowed)
