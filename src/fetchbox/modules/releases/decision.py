"""Decision engine: is a search result acceptable for what we want, and which is best.

``decide`` runs every check on one candidate and records *all* rejections (interactive
search shows each reason, not just the first). The checks, in the spec's order:

1. blocklisted, 2. already grabbed, 3. wrong title / year / media type / episode
(including the season-pack rule), 4. size outside the quality's bounds (and samples),
5. too few seeders, 6. quality not in the profile, 7. format score below the minimum,
8. language mismatch, then not an upgrade of the existing file, then user rules.

``rank`` orders decisions best first: accepted before rejected, then ladder tier,
revision (PROPER/REPACK), custom-format score, seeders (order of magnitude), indexer
priority (lower wins), closeness to the preferred size, exact seeders.

``select`` is what an automatic search grabs: the top movie release, or for series the
best season packs first ("a pack wins") and then the best single releases for the
episodes no pick covers yet, never two picks for the same episode.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, PrivateAttr, computed_field

from fetchbox.modules.profiles.formats import FormatSpec, ReleaseInfo
from fetchbox.modules.profiles.profile import ProfileSpec, evaluate, language_ok
from fetchbox.modules.releases.parser import ParsedRelease, normalize_title, parse_release

EpisodeKey = tuple[int, int]  # (season, episode)

RejectionCode = Literal[
    "blocklisted",
    "already_grabbed",
    "wrong_title",
    "wrong_year",
    "wrong_media_type",
    "wrong_episode",
    "season_not_finished",
    "season_pack_not_wanted",
    "size_too_small",
    "size_too_large",
    "sample",
    "too_few_seeders",
    "quality_not_allowed",
    "format_score_too_low",
    "language",
    "upgrades_disabled",
    "cutoff_met",
    "not_an_upgrade",
    "rule",
]


# ---- inputs ---------------------------------------------------------------------------------


class Candidate(BaseModel):
    """One search result as an indexer reported it."""

    model_config = ConfigDict(frozen=True)

    title: str
    source_id: str = ""
    source_name: str = ""
    priority: int = 25  # indexer priority; lower wins
    guid: str | None = None
    info_hash: str | None = None
    download_url: str | None = None
    size: int | None = None  # bytes
    seeders: int | None = None
    leechers: int | None = None
    published_at: datetime | None = None
    indexer_flags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ReleaseKey:
    """Identifies a release in the blocklist or grab history."""

    info_hash: str | None = None
    guid: str | None = None
    title: str | None = None

    def matches(self, c: Candidate) -> bool:
        if self.info_hash and c.info_hash:
            return self.info_hash.lower() == c.info_hash.lower()
        if self.guid and c.guid:
            return self.guid == c.guid
        return bool(self.title) and self.title == c.title


@dataclass(frozen=True)
class Existing:
    """The file a wanted item already has."""

    quality: str
    format_score: int = 0
    revision: int = 1


@dataclass(frozen=True)
class WantedEpisode:
    season: int
    episode: int
    absolute: int | None = None
    air_date: date | None = None
    existing: Existing | None = None

    @property
    def key(self) -> EpisodeKey:
        return (self.season, self.episode)


@dataclass(frozen=True)
class SeasonInfo:
    episode_count: int
    finished: bool  # every episode has aired


@dataclass(frozen=True)
class Target:
    """What the search is for: a movie, or some episodes of one series."""

    kind: Literal["movie", "series"]
    names: tuple[str, ...]  # primary first, then alternate / scene titles
    year: int | None = None
    original_language: str | None = None
    runtime_minutes: int | None = None  # per episode for series
    existing: Existing | None = None  # movies
    episodes: tuple[WantedEpisode, ...] = ()  # series: the episodes being searched for
    seasons: Mapping[int, SeasonInfo] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.names:
            raise ValueError("a target needs at least one name")


Rule = Callable[[Candidate, ParsedRelease, Target], str | None]
"""A user automation rule: returns a rejection message, or None to accept."""


@dataclass(frozen=True)
class DecisionContext:
    profile: ProfileSpec
    formats: Sequence[FormatSpec] = ()
    blocklist: Sequence[ReleaseKey] = ()
    grabbed: Sequence[ReleaseKey] = ()
    min_seeders: int = 1
    season_pack_share: float = 1.0  # share of a season's episodes that must be wanted
    rules: Sequence[Rule] = ()
    year_tolerance: int = 1


# ---- outputs --------------------------------------------------------------------------------


class Rejection(BaseModel):
    code: RejectionCode
    message: str


class MatchedFormat(BaseModel):
    id: str
    name: str
    score: int


class Decision(BaseModel):
    candidate: Candidate
    parsed: ParsedRelease
    quality: str
    tier: str | None
    format_score: int
    matched_formats: list[MatchedFormat]
    cutoff_met: bool
    episodes: list[EpisodeKey] = []  # wanted episodes this release covers
    season_pack: bool = False
    rejections: list[Rejection] = []

    _key: tuple[object, ...] = PrivateAttr(default=())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def accepted(self) -> bool:
        return not self.rejections


# ---- the checks -----------------------------------------------------------------------------

_ARTICLE = re.compile(r"^(?:the|a|an) ")
_YEAR_SUFFIX = re.compile(r"\s*\((?:19|20)\d\d\)\s*$")


def title_key(name: str) -> str:
    """Comparison key for titles: normalized, year suffix and leading article dropped."""
    return _ARTICLE.sub("", normalize_title(_YEAR_SUFFIX.sub("", name)))


def _fmt_ep(keys: Iterable[EpisodeKey]) -> str:
    return ", ".join(f"S{s:02d}E{e:02d}" for s, e in sorted(keys))


def _describe(p: ParsedRelease) -> str:
    if p.complete_series:
        return "the complete series"
    if p.full_season:
        return "season " + ", ".join(map(str, p.seasons))
    if p.episodes and p.seasons:
        return _fmt_ep((p.seasons[0], e) for e in p.episodes)
    if p.absolute_episodes:
        return "absolute episode " + ", ".join(map(str, p.absolute_episodes))
    if p.air_date:
        return p.air_date.isoformat()
    return "no episode"


@dataclass
class _Coverage:
    episodes: set[EpisodeKey]
    pack_seasons: list[int]  # empty unless this is a season pack / complete series
    episode_count: int | None  # episodes in the release, for size checks


def _coverage(p: ParsedRelease, t: Target) -> _Coverage:
    wanted = t.episodes
    if p.complete_series or p.full_season:
        seasons = (
            sorted(set(p.seasons))
            if p.full_season
            else sorted(set(t.seasons) | {w.season for w in wanted})
        )
        known = [t.seasons[s].episode_count for s in seasons if s in t.seasons]
        count = sum(known) if len(known) == len(seasons) else None
        covered = {w.key for w in wanted if w.season in seasons}
        return _Coverage(covered, seasons, count)
    if p.episodes and p.seasons:
        season = p.seasons[0]
        covered = {w.key for w in wanted if w.season == season and w.episode in p.episodes}
        return _Coverage(covered, [], len(p.episodes))
    if p.absolute_episodes:
        nums = set(p.absolute_episodes)
        covered = {w.key for w in wanted if w.absolute in nums}
        return _Coverage(covered, [], len(p.absolute_episodes))
    if p.air_date:
        covered = {w.key for w in wanted if w.air_date == p.air_date}
        return _Coverage(covered, [], 1)
    return _Coverage(set(), [], None)


def _cutoff_met(profile: ProfileSpec, quality: str, score: int) -> bool:
    idx = profile.tier_index(quality)
    return (
        idx is not None
        and idx >= profile.cutoff_index
        and (profile.cutoff_format_score is None or score >= profile.cutoff_format_score)
    )


def _upgrade_rejection(
    profile: ProfileSpec, ex: Existing, tier: int, revision: int, score: int
) -> Rejection | None:
    if not profile.upgrade_allowed:
        return Rejection(code="upgrades_disabled", message="the profile does not allow upgrades")
    if _cutoff_met(profile, ex.quality, ex.format_score):
        return Rejection(
            code="cutoff_met",
            message=f"the existing {ex.quality} file already meets the cutoff",
        )
    current = profile.tier_index(ex.quality)
    if current is None or tier > current:
        return None
    if tier == current:
        if revision > ex.revision:
            return None
        need = ex.format_score + profile.upgrade_min_score_increment
        if score >= need:
            return None
        return Rejection(
            code="not_an_upgrade",
            message=(
                f"same tier as the existing {ex.quality} file and format score {score} is "
                f"below {need}"
            ),
        )
    return Rejection(
        code="not_an_upgrade",
        message=f"{profile.ladder[tier].name} is below the existing {ex.quality} file",
    )


def decide(candidate: Candidate, target: Target, ctx: DecisionContext) -> Decision:
    parsed = parse_release(candidate.title)
    profile = ctx.profile
    info = ReleaseInfo(parsed, candidate.size, frozenset(candidate.indexer_flags))
    ev = evaluate(profile, info, ctx.formats)
    out: list[Rejection] = []

    def reject(code: RejectionCode, message: str) -> None:
        out.append(Rejection(code=code, message=message))

    # 1-2. blocklist and grab history
    if any(k.matches(candidate) for k in ctx.blocklist):
        reject("blocklisted", "this release is blocklisted")
    if any(k.matches(candidate) for k in ctx.grabbed):
        reject("already_grabbed", "this release was already grabbed")

    # 3. title, year, media type, episodes
    keys = {title_key(n) for n in target.names}
    if not keys & {title_key(n) for n in (parsed.title, *parsed.alt_titles)}:
        reject("wrong_title", f"title {parsed.title!r} does not match {target.names[0]!r}")
    if (
        parsed.year is not None
        and target.year is not None
        and abs(parsed.year - target.year) > ctx.year_tolerance
    ):
        reject("wrong_year", f"year {parsed.year} does not match {target.year}")

    coverage = _Coverage(set(), [], 1)
    if target.kind == "movie":
        if parsed.media_type == "series":
            reject("wrong_media_type", f"this is a TV release ({_describe(parsed)})")
    elif parsed.media_type == "movie":
        reject("wrong_media_type", "this is not a TV release (no season or episode)")
    else:
        coverage = _coverage(parsed, target)
        if not coverage.episodes:
            reject("wrong_episode", f"{_describe(parsed)} is not wanted")
        wanted_by_season: dict[int, int] = {}
        for w in target.episodes:
            wanted_by_season[w.season] = wanted_by_season.get(w.season, 0) + 1
        for s in coverage.pack_seasons:
            info_s = target.seasons.get(s)
            if info_s is None:
                continue  # unknown season shape: nothing to compare against
            if not info_s.finished:
                reject("season_not_finished", f"season {s} has not finished airing")
            wanted_n = wanted_by_season.get(s, 0)
            if info_s.episode_count and wanted_n / info_s.episode_count < ctx.season_pack_share:
                reject(
                    "season_pack_not_wanted",
                    f"only {wanted_n} of {info_s.episode_count} episodes in season {s} are "
                    f"wanted (a pack needs {ctx.season_pack_share:.0%})",
                )

    # 4. size, samples
    mb_per_min: float | None = None
    runtime = target.runtime_minutes
    if candidate.size is not None and runtime and coverage.episode_count:
        mb_per_min = candidate.size / 1_000_000 / (runtime * coverage.episode_count)
        limit = profile.size_limit(ev.quality)
        if mb_per_min < limit.min:
            reject(
                "size_too_small",
                f"{mb_per_min:.1f} MB/min is below the {ev.quality} minimum of {limit.min:g}",
            )
        elif limit.max is not None and mb_per_min > limit.max:
            reject(
                "size_too_large",
                f"{mb_per_min:.1f} MB/min is above the {ev.quality} maximum of {limit.max:g}",
            )
    if parsed.sample:
        reject("sample", "this is a sample")

    # 5. seeders
    if candidate.seeders is not None and candidate.seeders < ctx.min_seeders:
        reject("too_few_seeders", f"{candidate.seeders} seeders (minimum {ctx.min_seeders})")

    # 6-8. profile: quality, format score, language
    if not ev.allowed:
        reject("quality_not_allowed", f"{ev.quality} is not in profile {profile.name!r}")
    if not ev.meets_min_score:
        why = ", ".join(f"{m.name} {m.score:+d}" for m in ev.format_score.matched)
        reject(
            "format_score_too_low",
            f"format score {ev.format_score.total} is below the minimum "
            f"{profile.min_format_score}" + (f" ({why})" if why else ""),
        )
    if not language_ok(profile, parsed, target.original_language):
        langs = ", ".join(parsed.languages) or "unknown"
        reject("language", f"language {langs} is not allowed by the profile")

    # upgrades: the release must beat every existing file it would replace
    if ev.tier_index is not None:
        existing = (
            [target.existing]
            if target.kind == "movie"
            else [w.existing for w in target.episodes if w.key in coverage.episodes]
        )
        for ex in existing:
            if ex is None:
                continue
            r = _upgrade_rejection(
                profile, ex, ev.tier_index, parsed.revision.version, ev.format_score.total
            )
            if r is not None:
                out.append(r)
                break

    # user rules
    for rule in ctx.rules:
        message = rule(candidate, parsed, target)
        if message:
            reject("rule", message)

    d = Decision(
        candidate=candidate,
        parsed=parsed,
        quality=ev.quality,
        tier=ev.tier,
        format_score=ev.format_score.total,
        matched_formats=[MatchedFormat(**m.__dict__) for m in ev.format_score.matched],
        cutoff_met=ev.cutoff_met,
        episodes=sorted(coverage.episodes),
        season_pack=bool(coverage.pack_seasons),
        rejections=out,
    )
    preferred = profile.size_limit(ev.quality).preferred
    seeders = candidate.seeders or 0
    d._key = (
        bool(out),  # accepted first
        -(ev.tier_index if ev.tier_index is not None else -1),
        -parsed.revision.version,
        -ev.format_score.total,
        -(round(math.log10(seeders)) if seeders > 0 else -1),
        candidate.priority,
        abs(mb_per_min - preferred) if preferred is not None and mb_per_min is not None else 0,
        -seeders,
        candidate.title,
    )
    return d


def rank(decisions: Iterable[Decision]) -> list[Decision]:
    """Best first; rejected decisions follow the accepted ones in the same order."""
    return sorted(decisions, key=lambda d: d._key)


def select(ranked: Sequence[Decision], target: Target) -> list[Decision]:
    """What an automatic search grabs, from ``rank``'s output."""
    accepted = [d for d in ranked if d.accepted]
    if target.kind == "movie":
        return accepted[:1]
    picks: list[Decision] = []
    covered: set[EpisodeKey] = set()
    for packs_pass in (True, False):
        for d in accepted:
            eps = set(d.episodes)
            if d.season_pack == packs_pass and eps and not eps & covered:
                picks.append(d)
                covered |= eps
    return picks


def decide_all(
    candidates: Iterable[Candidate], target: Target, ctx: DecisionContext
) -> list[Decision]:
    return rank(decide(c, target, ctx) for c in candidates)
