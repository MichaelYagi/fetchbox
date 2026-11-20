"""Quality catalog, custom-format matching and profile evaluation (no database)."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from fetchbox.modules.profiles import defaults
from fetchbox.modules.profiles.formats import (
    Condition,
    FormatSpec,
    ReleaseInfo,
    format_matches,
    score_release,
)
from fetchbox.modules.profiles.profile import ProfileSpec, evaluate, language_ok
from fetchbox.modules.profiles.qualities import BY_NAME, SizeLimit, default_size, size_ok
from fetchbox.modules.releases.parser import parse_release, quality_name

CONDS: TypeAdapter[list[Condition]] = TypeAdapter(list[Condition])


def info(name: str, size: int | None = None, flags: frozenset[str] = frozenset()) -> ReleaseInfo:
    return ReleaseInfo(parse_release(name), size, flags)


def fmt(conditions: list[dict[str, Any]], *, id: str = "f", score: int = 0) -> FormatSpec:
    return FormatSpec(id, id, tuple(CONDS.validate_python(conditions)), score)


# ---- catalog --------------------------------------------------------------------------------


def test_every_parser_quality_is_in_the_catalog() -> None:
    sources = [None, "cam", "telesync", "telecine", "workprint", "screener", "regional", "sdtv",
               "hdtv", "dvd", "webrip", "webdl", "bluray"]  # fmt: skip
    resolutions = [None, 240, 360, 480, 576, 720, 1080, 1440, 2160, 4320]
    for src, res, mod in itertools.product(
        sources, resolutions, [None, "remux", "brdisk", "rawhd"]
    ):
        name = quality_name(src, res, mod)
        assert name in BY_NAME, (src, res, mod, name)


def test_every_corpus_name_parses_to_a_catalog_quality() -> None:
    from tests.test_release_parser import CASES

    for p in CASES:
        assert parse_release(p.values[0]["name"]).quality in BY_NAME


def test_size_bounds() -> None:
    web1080 = default_size("WEBDL-1080p")
    two_hours = 120
    assert size_ok(web1080, 4_000_000_000, two_hours)
    assert not size_ok(web1080, 200_000_000, two_hours)  # 1.7 MB/min: too small (fake/sample)
    assert not size_ok(web1080, 40_000_000_000, two_hours)  # 333 MB/min: too big for WEB
    assert size_ok(default_size("Remux-2160p"), 90_000_000_000, two_hours)  # no max
    assert size_ok(web1080, 1, 0)  # unknown runtime never rejects
    with pytest.raises(ValidationError):
        SizeLimit(min=10, max=5)
    with pytest.raises(ValidationError):
        SizeLimit(min=10, preferred=50, max=20)


# ---- format matching ------------------------------------------------------------------------

DV = "Movie.2020.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.HEVC-FLUX"
HD = "Movie.2020.1080p.BluRay.x264.DTS-SPARKS"


def test_value_condition() -> None:
    f = fmt([{"type": "hdr", "values": ["DV"]}])
    assert format_matches(f.conditions, info(DV))
    assert not format_matches(f.conditions, info(HD))


def test_same_type_conditions_are_ored_different_types_anded() -> None:
    # (group FLUX or group SPARKS) and 2160p
    conds = fmt(
        [
            {"type": "release_group", "pattern": "^FLUX$"},
            {"type": "release_group", "pattern": "^SPARKS$"},
            {"type": "resolution", "values": [2160]},
        ]
    ).conditions
    assert format_matches(conds, info(DV))
    assert not format_matches(conds, info(HD))  # SPARKS but 1080p
    assert not format_matches(conds, info("Movie.2020.2160p.WEB-DL-OTHER"))


def test_required_and_negate() -> None:
    # x265 that is NOT 2160p (the classic "x265 in HD" format)
    conds = fmt(
        [
            {"type": "video_codec", "values": ["h265"], "required": True},
            {"type": "resolution", "values": [2160], "negate": True, "required": True},
        ]
    ).conditions
    assert format_matches(conds, info("Movie.2020.1080p.WEB-DL.x265-GRP"))
    assert not format_matches(conds, info("Movie.2020.2160p.WEB-DL.x265-GRP"))
    assert not format_matches(conds, info("Movie.2020.1080p.WEB-DL.x264-GRP"))


def test_pattern_condition_is_case_insensitive_search() -> None:
    conds = fmt([{"type": "release_title", "pattern": r"\bimax\b"}]).conditions
    assert format_matches(conds, info("Movie.2020.IMAX.1080p.WEB-DL-GRP"))
    assert not format_matches(conds, info("Imaxine.2020.1080p.WEB-DL-GRP"))


def test_edition_and_flags_and_language() -> None:
    assert format_matches(
        fmt([{"type": "edition", "pattern": "director"}]).conditions,
        info("Alien.1979.Directors.Cut.1080p.BluRay.x264-GRP"),
    )
    assert format_matches(
        fmt([{"type": "flag", "values": ["repack", "proper"]}]).conditions,
        info("Movie.2020.REPACK.1080p.WEB-DL-GRP"),
    )
    assert format_matches(
        fmt([{"type": "language", "values": ["fr"]}]).conditions,
        info("Movie.2020.MULTi.VFF.1080p.BluRay.x264-GRP"),
    )


def test_size_and_indexer_flag_conditions() -> None:
    small = fmt([{"type": "size", "max_mb": 1000}]).conditions
    assert format_matches(small, info(HD, size=700_000_000))
    assert not format_matches(small, info(HD, size=7_000_000_000))
    assert not format_matches(small, info(HD))  # unknown size never matches
    negated = fmt([{"type": "size", "max_mb": 1000, "negate": True}]).conditions
    assert not format_matches(negated, info(HD))  # ...not even when negated
    fl = fmt([{"type": "indexer_flag", "values": ["freeleech"]}]).conditions
    assert format_matches(fl, info(HD, flags=frozenset({"freeleech"})))


def test_empty_format_never_matches() -> None:
    assert not format_matches((), info(HD))


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "hdr", "values": ["HDR12"]},
        {"type": "resolution", "values": [1081]},
        {"type": "language", "values": ["english"]},
        {"type": "release_title", "pattern": "(unclosed"},
        {"type": "size"},
        {"type": "size", "min_mb": 5, "max_mb": 1},
        {"type": "nope", "values": ["x"]},
        {"type": "source", "values": []},
    ],
)
def test_invalid_conditions_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CONDS.validate_python([bad])


def test_scores_use_profile_override_then_default() -> None:
    formats = [
        fmt([{"type": "hdr", "values": ["DV"]}], id="dv", score=10),
        fmt([{"type": "flag", "values": ["atmos"]}], id="atmos", score=5),
        fmt([{"type": "audio_codec", "values": ["dts"]}], id="dts", score=3),
    ]
    s = score_release(info(DV), formats, {"dv": 25})
    assert s.total == 30
    assert [(m.id, m.score) for m in s.matched] == [("dv", 25), ("atmos", 5)]


# ---- profiles -------------------------------------------------------------------------------


def _hd(**kw: Any) -> ProfileSpec:
    return ProfileSpec.model_validate({**defaults.PROFILES[0], **kw})


def test_default_profiles_are_valid() -> None:
    for p in defaults.PROFILES:
        ProfileSpec.model_validate(p | {"format_scores": {}})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"cutoff": "Nope"}, "cutoff"),
        ({"ladder": [{"name": "A", "qualities": ["WEBDL-1080p"]},
                     {"name": "A", "qualities": ["Bluray-1080p"]}], "cutoff": "A"}, "unique"),
        ({"ladder": [{"name": "A", "qualities": ["WEBDL-1080p"]},
                     {"name": "B", "qualities": ["WEBDL-1080p"]}], "cutoff": "A"}, "more than one"),
        ({"ladder": [{"name": "A", "qualities": ["WEBDL-1081p"]}], "cutoff": "A"}, "unknown"),
        ({"languages": ["english"]}, "ISO 639-1"),
        ({"sizes": {"Nope": {"min": 1}}}, "sizes"),
        ({"ladder": []}, "at least 1"),
    ],
)  # fmt: skip
def test_profile_validation(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _hd(**change)


def test_evaluate_tiers_and_cutoff() -> None:
    hd = _hd()
    web = evaluate(hd, info("Movie.2020.1080p.WEBRip.x264-GRP"), [])
    assert web.allowed and web.tier == "WEB 1080p" and web.cutoff_met
    webdl = evaluate(hd, info("Movie.2020.1080p.WEB-DL.x264-GRP"), [])
    assert webdl.tier_index == web.tier_index  # same tier ranks equal
    low = evaluate(hd, info("Movie.2020.720p.HDTV.x264-GRP"), [])
    assert low.allowed and not low.cutoff_met and low.tier_index is not None
    assert low.tier_index < web.tier_index
    cam = evaluate(hd, info("Movie.2020.CAM.x264-GRP"), [])
    assert not cam.allowed and cam.tier is None and not cam.cutoff_met
    uhd = evaluate(hd, info("Movie.2020.2160p.WEB-DL.x265-GRP"), [])
    assert not uhd.allowed  # Standard HD doesn't include 4K


def test_cutoff_format_score_and_min_score() -> None:
    dv = fmt([{"type": "hdr", "values": ["DV"]}], id="dv", score=10)
    hs = fmt([{"type": "flag", "values": ["hardcoded_subs"]}], id="hc", score=-10000)
    four_k = ProfileSpec.model_validate(
        defaults.PROFILES[1] | {"format_scores": {}, "cutoff_format_score": 10}
    )
    plain = evaluate(four_k, info("Movie.2020.2160p.WEB-DL.HDR.HEVC-GRP"), [dv, hs])
    assert plain.allowed and not plain.cutoff_met  # tier reached, score not
    with_dv = evaluate(four_k, info(DV), [dv, hs])
    assert with_dv.cutoff_met and with_dv.format_score.total == 10
    hardsub = evaluate(four_k, info("Movie.2020.2160p.WEB-DL.HC.HEVC-GRP"), [dv, hs])
    assert not hardsub.meets_min_score


@pytest.mark.parametrize(
    ("languages", "name", "original", "ok"),
    [
        (["original"], "Movie.2020.1080p.WEB-DL-GRP", "en", True),  # unstated = original
        (["original"], "Movie.2020.FRENCH.1080p.WEB-DL-GRP", "en", False),
        (["original"], "Movie.2020.FRENCH.1080p.WEB-DL-GRP", "fr", True),
        (["original"], "Movie.2020.MULTi.VFF.1080p.WEB-DL-GRP", "en", True),  # multi has orig
        (["original"], "Movie.2020.FRENCH.1080p.WEB-DL-GRP", None, False),
        (["fr"], "Movie.2020.1080p.WEB-DL-GRP", "en", False),  # unstated = English here
        (["fr"], "Movie.2020.1080p.WEB-DL-GRP", None, True),  # can't tell: don't reject
        (["fr"], "Movie.2020.TRUEFRENCH.1080p.WEB-DL-GRP", "en", True),
        (["original", "de"], "Movie.2020.GERMAN.DL.1080p.WEB-DL-GRP", "ja", True),
        ([], "Movie.2020.HINDI.1080p.WEB-DL-GRP", "en", True),  # empty = any
    ],
)
def test_language_ok(languages: list[str], name: str, original: str | None, ok: bool) -> None:
    assert language_ok(_hd(languages=languages), parse_release(name), original) is ok
