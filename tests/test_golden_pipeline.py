"""Golden results for movies, TV and music: every real release name in the fixtures, run
through parsing, the release decision (against each built-in profile) and naming (each
preset), compared with tests/fixtures/golden/*.json.

It pins what Fetchbox does today, so a refactor (the plugin work above all) that changes
any result for the core media types fails here, naming the release and what changed.

A change that's meant: regenerate with ``FETCHBOX_UPDATE_GOLDEN=1 pytest
tests/test_golden_pipeline.py`` and review the diff of the JSON in the pull request.
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import TypeAdapter

from fetchbox.modules.library.music_files import Naming, WantedTrack, track_path
from fetchbox.modules.library.naming import PRESETS, episode_path, movie_path
from fetchbox.modules.library.service import episode_context, movie_context
from fetchbox.modules.profiles.defaults import FORMATS, MUSIC_PROFILES, PROFILES
from fetchbox.modules.profiles.formats import Condition, FormatSpec
from fetchbox.modules.profiles.profile import ProfileSpec
from fetchbox.modules.releases.decision import (
    Candidate,
    DecisionContext,
    SeasonInfo,
    Target,
    WantedEpisode,
    decide,
)
from fetchbox.modules.releases.music import match_album, parse_music
from fetchbox.modules.releases.parser import ParsedRelease, parse_release
from tests.test_music_names import NAMES, WANT

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden"
UPDATE = os.environ.get("FETCHBOX_UPDATE_GOLDEN") == "1"
GB = 1_000_000_000

_CONDS: TypeAdapter[list[Condition]] = TypeAdapter(list[Condition])
VIDEO_FORMATS = tuple(
    FormatSpec(slug, f["name"], tuple(_CONDS.validate_python(f["conditions"])), f["default_score"])
    for slug, f in FORMATS.items()
)
VIDEO_PROFILES = {p["name"]: ProfileSpec.model_validate(p) for p in PROFILES}
MUSIC_SPECS = {p["name"]: ProfileSpec.model_validate(p) for p in MUSIC_PROFILES}


def _cases() -> list[dict[str, Any]]:
    out = []
    for path in sorted((FIXTURES / "releases").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("//"):
                out.append(json.loads(line))
    return out


def _decisions(case: dict[str, Any], p: ParsedRelease) -> dict[str, Any]:
    names = (case["title"],)
    if p.media_type == "series":
        wanted = [
            WantedEpisode(s, e, absolute=a)
            for s in (p.seasons or [1])
            for e, a in zip(
                p.episodes or list(range(1, 11)),
                p.absolute_episodes or [None] * len(p.episodes or range(1, 11)),
                strict=False,
            )
        ]
        if p.absolute_episodes and not p.episodes:
            wanted = [WantedEpisode(1, n, absolute=n) for n in p.absolute_episodes]
        seasons = {s: SeasonInfo(10, True) for s in (p.seasons or [1])}
        target = Target("series", names, case.get("year"), "en", 45, episodes=tuple(wanted),
                        seasons=seasons)  # fmt: skip
        size = 1.5 * GB * max(1, len(wanted) if not p.episodes else len(p.episodes))
    else:
        target = Target("movie", names, case.get("year") or p.year, "en", runtime_minutes=120)
        size = 6 * GB
    out = {}
    for name, profile in VIDEO_PROFILES.items():
        d = decide(
            Candidate(title=case["name"], size=int(size), seeders=50),
            target,
            DecisionContext(profile=profile, formats=VIDEO_FORMATS),
        )
        out[name] = {
            "accepted": d.accepted,
            "rejections": [r.code for r in d.rejections],
            "quality": d.quality,
            "tier": d.tier,
            "score": d.format_score,
            "formats": [m.id for m in d.matched_formats],
            "cutoff_met": d.cutoff_met,
            "episodes": [list(k) for k in d.episodes],
            "season_pack": d.season_pack,
        }
    return out


def _paths(case: dict[str, Any], p: ParsedRelease) -> dict[str, str]:
    title = SimpleNamespace(name=case["title"], year=case.get("year") or p.year or 2020,
                            tmdb_id=12345, imdb_id="tt0012345", tvdb_id=67890)  # fmt: skip
    out = {}
    for preset, patterns in PRESETS.items():
        if p.media_type == "series":
            eps = [
                SimpleNamespace(number=e, name=f"Episode {e}", air_date=date(2020, 1, e % 28 + 1))
                for e in (p.episodes or [1])
            ]
            season = (p.seasons or [1])[0]
            ctx = episode_context(title, p, p.quality, None, season, eps)  # type: ignore[arg-type]
            out[preset] = episode_path(patterns, ctx, "mkv")
        else:
            ctx = movie_context(title, p, p.quality)  # type: ignore[arg-type]
            out[preset] = movie_path(patterns, ctx, "mkv")
    return out


def _video() -> dict[str, Any]:
    results: dict[str, Any] = {}
    for case in _cases():
        p = parse_release(case["name"])
        results[case["name"]] = {
            "parsed": p.model_dump(mode="json"),
            "decisions": _decisions(case, p),
            "paths": _paths(case, p),
        }
    return results


def _music() -> dict[str, Any]:
    results: dict[str, Any] = {}
    namings = {
        "default": Naming(),
        "flat": Naming(
            folder="artist - album (year)", track="artist - nn - title", disc_folders=False
        ),
        "no year": Naming(folder="artist/album", track="nn - title"),
    }
    track = WantedTrack(id="t3", disc=2, number=3, name="Teardrop", length_ms=330_000)
    for search, rows in sorted(NAMES.items()):
        for name, size in rows:
            m = parse_music(name)
            entry: dict[str, Any] = {"parsed": dataclasses.asdict(m)}
            if search in WANT:
                artist, album = WANT[search]
                hit = match_album(name, artist, album)
                entry["match"] = {"ok": hit.ok, "reason": hit.reason}
                target = Target("music", (artist,), album=album, album_tracks=11,
                                album_ms=63 * 60_000)  # fmt: skip
                for pname, profile in MUSIC_SPECS.items():
                    d = decide(Candidate(title=name, size=size, seeders=20), target,
                               DecisionContext(profile=profile))  # fmt: skip
                    entry.setdefault("decisions", {})[pname] = {
                        "accepted": d.accepted,
                        "rejections": [r.code for r in d.rejections],
                        "quality": d.quality,
                    }
            results[f"{search} | {name}"] = entry
    results["(naming)"] = {
        k: track_path("Massive Attack", "Mezzanine", 1998, track, 2, ".flac", n)
        for k, n in namings.items()
    }
    return results


@pytest.mark.parametrize("name,build", [("video", _video), ("music", _music)])
def test_core_media_results_are_unchanged(name: str, build: Any) -> None:
    got = json.loads(json.dumps(build(), sort_keys=True))
    path = GOLDEN / f"{name}.json"
    if UPDATE or not path.exists():
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                        encoding="utf-8")  # fmt: skip
        if not UPDATE:
            pytest.fail(f"{path.name} was missing and has been written: commit it")
        return
    want = json.loads(path.read_text(encoding="utf-8"))
    changed = [k for k in sorted(set(want) | set(got)) if want.get(k) != got.get(k)]
    detail = "\n".join(_diff(k, want.get(k), got.get(k)) for k in changed[:5])
    assert not changed, f"{len(changed)} result(s) changed for {name}:\n{detail}"


def _leaves(v: Any, at: str = "") -> dict[str, Any]:
    if isinstance(v, dict) and v:
        out: dict[str, Any] = {}
        for k, x in v.items():
            out |= _leaves(x, f"{at}.{k}" if at else str(k))
        return out
    return {at: v}


def _diff(key: str, was: Any, now: Any) -> str:
    a, b = _leaves(was), _leaves(now)
    lines = [f"- {key}"]
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            lines.append(f"    {k}: {a.get(k)!r} -> {b.get(k)!r}")
    return "\n".join(lines[:12])
