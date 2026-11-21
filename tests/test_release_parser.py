"""Fixture-driven release parser tests.

Each line of ``tests/fixtures/releases/*.jsonl`` is one case. ``title`` is compared after
``normalize_title`` (so punctuation style doesn't matter); every other key present is
compared exactly. ``"xfail": "reason"`` documents a known gap; the mark is strict, so a
fix that makes it pass must also remove the marker.
"""

from __future__ import annotations

import json
import random
import string
from pathlib import Path
from typing import Any

import pytest

from fetchbox.modules.releases.parser import (
    ParsedRelease,
    _parse,
    normalize_title,
    parse_release,
    quality_name,
)

FIXTURES = Path(__file__).parent / "fixtures" / "releases"

# fixture key -> how to read it from the result
FIELD_MAP: dict[str, Any] = {
    "type": lambda r: r.media_type,
    "year": "year",
    "quality": "quality",
    "resolution": "resolution",
    "source": "source",
    "modifier": "modifier",
    "seasons": "seasons",
    "episodes": "episodes",
    "absolute": "absolute_episodes",
    "air_date": lambda r: r.air_date.isoformat() if r.air_date else None,
    "full_season": "full_season",
    "complete": "complete_series",
    "codec": "video_codec",
    "bit_depth": "bit_depth",
    "hdr": "hdr",
    "audio": "audio_codecs",
    "channels": "audio_channels",
    "atmos": "atmos",
    "service": "streaming_service",
    "edition": "edition",
    "languages": "languages",
    "multi": "multi_language",
    "subs": "subtitle_languages",
    "hardsub": "hardcoded_subs",
    "dubbed": "dubbed",
    "group": "group",
    "version": lambda r: r.revision.version,
    "real": lambda r: r.revision.real,
    "proper": "proper",
    "repack": "repack",
    "internal": "internal",
    "limited": "limited",
    "three_d": "three_d",
    "hybrid": "hybrid",
    "sample": "sample",
    "hash": "hash",
    "ext": "extension",
    "alt_titles": lambda r: [normalize_title(t) for t in r.alt_titles],
}


def _load() -> list[Any]:
    cases = []
    for path in sorted(FIXTURES.glob("*.jsonl")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("//"):
                continue
            case = json.loads(line)
            marks = []
            if "xfail" in case:
                marks = [pytest.mark.xfail(reason=case["xfail"], strict=True)]
            cases.append(pytest.param(case, id=f"{path.stem}:{lineno}", marks=marks))
    return cases


CASES = _load()


def _get(result: ParsedRelease, key: str) -> Any:
    getter = FIELD_MAP[key]
    return getter(result) if callable(getter) else getattr(result, getter)


@pytest.mark.parametrize("case", CASES)
def test_fixture(case: dict[str, Any]) -> None:
    result = parse_release(case["name"])
    mismatches = []
    if normalize_title(result.title) != normalize_title(case["title"]):
        mismatches.append(f"title: got {result.title!r}, want {case['title']!r}")
    for key, want in case.items():
        if key in ("name", "title", "xfail"):
            continue
        if key == "alt_titles":
            want = [normalize_title(t) for t in want]
        got = _get(result, key)
        if got != want:
            mismatches.append(f"{key}: got {got!r}, want {want!r}")
    assert not mismatches, f"{case['name']!r}\n  " + "\n  ".join(mismatches)


def test_corpus_is_well_formed() -> None:
    allowed = {"name", "title", "xfail", *FIELD_MAP}
    names = []
    for param in CASES:
        case = param.values[0]
        assert {"name", "title", "type"} <= case.keys(), case
        assert case.keys() <= allowed, set(case) - allowed
        names.append(case["name"])
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate fixtures: {sorted(dupes)}"


def test_corpus_size() -> None:
    assert len(CASES) >= 500


def test_corpus_pass_rate() -> None:
    known_gaps = sum(1 for p in CASES if p.marks)
    assert known_gaps / len(CASES) < 0.05, f"{known_gaps} known gaps"


# ---- unit tests beyond the corpus ---------------------------------------------------------


def test_display_title_keeps_acronyms_and_punctuation() -> None:
    assert parse_release("Marvels.Agents.of.S.H.I.E.L.D.S01E01.720p").title == (
        "Marvels Agents of S.H.I.E.L.D."
    )
    assert parse_release("L.A.Confidential.1997.1080p").title == "L.A. Confidential"
    assert parse_release("Spider-Man.No.Way.Home.2021.1080p").title == "Spider-Man No Way Home"
    assert parse_release("Mr. Robot (2015) S01E01 1080p").title == "Mr. Robot"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("S.H.I.E.L.D.", "SHIELD"),
        ("Law & Order", "Law and Order"),
        ("Grey's Anatomy", "Greys Anatomy"),
        ("Amélie", "Amelie"),
        ("9-1-1", "911"),
        ("Spider-Man", "Spider Man"),
    ],
)
def test_normalize_title_equivalences(a: str, b: str) -> None:
    assert normalize_title(a) == normalize_title(b)


def test_normalize_title_keeps_distinct_titles_distinct() -> None:
    assert normalize_title("Up") != normalize_title("Us")
    assert normalize_title("Heat") != normalize_title("The Heat")


@pytest.mark.parametrize(
    ("source", "res", "mod", "name"),
    [
        ("webdl", 1080, None, "WEBDL-1080p"),
        ("webdl", None, None, "WEBDL-480p"),
        ("hdtv", 480, None, "SDTV"),
        ("hdtv", None, None, "SDTV"),
        ("bluray", 2160, "remux", "Remux-2160p"),
        ("bluray", None, "brdisk", "BR-DISK"),
        ("dvd", None, "remux", "DVD-Remux"),
        (None, 720, None, "Unknown-720p"),
        (None, None, None, "Unknown"),
    ],
)
def test_quality_name(source: str | None, res: int | None, mod: str | None, name: str) -> None:
    assert quality_name(source, res, mod) == name


def test_result_is_json_serializable() -> None:
    r = parse_release("Show.2024.01.15.720p.WEB.h264-GRP")
    data = r.model_dump(mode="json")
    assert data["air_date"] == "2024-01-15"
    assert data["media_type"] == "series"
    assert ParsedRelease.model_validate(data).air_date == r.air_date


def test_never_raises_on_garbage() -> None:
    rng = random.Random(1234)
    alphabet = string.ascii_letters + string.digits + " ._-[](){}~+&!@#'\"/\\|:;,<>?*" + "é【】"
    tokens = ["S01E01", "1080p", "x264", "-GRP", "[ABCD1234]", "2020", "DTS-HD", "v2", " - 05"]
    for _ in range(3000):
        parts = [
            rng.choice(tokens) if rng.random() < 0.3 else rng.choice(alphabet)
            for _ in range(rng.randint(0, 60))
        ]
        name = "".join(parts)
        r = _parse(name)  # not parse_release: its safety net would hide crashes
        assert isinstance(r.title, str)
        assert r.raw == name


def test_parse_is_fast_enough() -> None:
    import time

    names = [p.values[0]["name"] for p in CASES]
    start = time.perf_counter()
    for _ in range(5):
        for n in names:
            parse_release(n)
    per_name = (time.perf_counter() - start) / (5 * len(names))
    # An indexer search returns ~100-1000 results; parsing must not be the bottleneck.
    assert per_name < 0.005, f"{per_name * 1000:.2f} ms per name"
