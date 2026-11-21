"""Decision engine: rejection checks, ranking, pack-vs-episode selection, search fan-out."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest
from pydantic import TypeAdapter

from fetchbox.modules.profiles.formats import Condition, FormatSpec
from fetchbox.modules.profiles.profile import ProfileSpec
from fetchbox.modules.releases.decision import (
    Candidate,
    DecisionContext,
    Existing,
    ReleaseKey,
    SeasonInfo,
    Target,
    WantedEpisode,
    decide,
    decide_all,
    select,
    title_key,
)
from fetchbox.modules.releases.search import SearchQuery, search
from tests.pipeline import STANDARD_HD

CONDS: TypeAdapter[list[Condition]] = TypeAdapter(list[Condition])
GB = 1_000_000_000

HARDSUB = FormatSpec("hc", "Hardcoded subtitles", tuple(CONDS.validate_python(
    [{"type": "flag", "values": ["hardcoded_subs"]}])), -10000)  # fmt: skip
ATMOS = FormatSpec("atmos", "Atmos", tuple(CONDS.validate_python(
    [{"type": "flag", "values": ["atmos"]}])), 5)  # fmt: skip


def hd(**kw: Any) -> ProfileSpec:
    return ProfileSpec.model_validate({**STANDARD_HD, "format_scores": {}, **kw})


def ctx(**kw: Any) -> DecisionContext:
    return DecisionContext(**{"profile": hd(), "formats": (HARDSUB, ATMOS), **kw})


DUNE = Target("movie", ("Dune", "Dune: Part One"), 2021, "en", runtime_minutes=155)


def c(title: str, size: float | None = 4 * GB, seeders: int | None = 50, **kw: Any) -> Candidate:
    return Candidate(title=title, size=int(size) if size else None, seeders=seeders, **kw)


def codes(
    title: str, target: Target = DUNE, cx: DecisionContext | None = None, **kw: Any
) -> list[str]:
    return [r.code for r in decide(c(title, **kw), target, cx or ctx()).rejections]


# ---- each check -----------------------------------------------------------------------------


def test_good_release_is_accepted() -> None:
    d = decide(c("Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"), DUNE, ctx())
    assert d.accepted and d.tier == "WEB 1080p" and d.format_score == 5 and d.cutoff_met
    assert [m.name for m in d.matched_formats] == ["Atmos"]


def test_blocklist_and_grab_history() -> None:
    name = "Dune.2021.1080p.WEB-DL-FLUX"
    by_hash = ReleaseKey(info_hash="ABC")
    assert codes(name, info_hash="abc", cx=ctx(blocklist=[by_hash])) == ["blocklisted"]
    assert codes(name, guid="g1", cx=ctx(grabbed=[ReleaseKey(guid="g1")])) == ["already_grabbed"]
    # A hash on both sides is authoritative: a different hash is a different torrent.
    assert codes(name, info_hash="def", guid="g1",
                 cx=ctx(blocklist=[ReleaseKey(info_hash="abc", guid="g1")])) == []  # fmt: skip
    assert codes(name, cx=ctx(blocklist=[ReleaseKey(title=name)])) == ["blocklisted"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Dune.2021.1080p.WEB-DL-FLUX", []),
        ("Dune.Part.One.2021.1080p.WEB-DL-FLUX", []),  # alternate title
        ("Dune.2022.1080p.WEB-DL-FLUX", []),  # within a year
        ("Dune.1984.1080p.BluRay.x264-GRP", ["wrong_year"]),
        ("Dune.1080p.WEB-DL-FLUX", []),  # no year: don't reject
        ("Dunes.2021.1080p.WEB-DL-FLUX", ["wrong_title"]),
        ("Dune.S01E01.1080p.WEB-DL-FLUX", ["wrong_media_type"]),
    ],
)
def test_title_year_media_type(name: str, expected: list[str]) -> None:
    assert codes(name) == expected


def test_title_key() -> None:
    assert title_key("The Office (US)") == title_key("Office US")
    assert title_key("Doctor Who (2005)") == title_key("Doctor.Who")
    assert title_key("Marvel's Agents of S.H.I.E.L.D.") == title_key("Marvels Agents of SHIELD")
    assert title_key("Le Fabuleux Destin d'Amélie") == title_key("Le.Fabuleux.Destin.d.Amelie")
    assert title_key("Dune") != title_key("Dunes")


def test_size_bounds_use_runtime_and_profile_overrides() -> None:
    assert codes("Dune.2021.1080p.WEB-DL-FLUX", size=0.2 * GB) == ["size_too_small"]
    assert codes("Dune.2021.1080p.WEB-DL-FLUX", size=80 * GB) == ["size_too_large"]
    assert codes("Dune.2021.1080p.WEB-DL-FLUX", size=None) == []  # unknown size
    no_runtime = Target("movie", ("Dune",), 2021, "en")
    assert codes("Dune.2021.1080p.WEB-DL-FLUX", no_runtime, size=0.2 * GB) == []
    roomy = ctx(profile=hd(sizes={"WEBDL-1080p": {"min": 0, "max": 1000}}))
    assert codes("Dune.2021.1080p.WEB-DL-FLUX", cx=roomy, size=80 * GB) == []


def test_sample_seeders_quality_score_language() -> None:
    assert "sample" in codes("Dune.2021.1080p.WEB-DL-FLUX.sample", size=None)
    assert codes("Dune.2021.1080p.WEB-DL-FLUX", seeders=0) == ["too_few_seeders"]
    assert codes("Dune.2021.1080p.WEB-DL-FLUX", seeders=None) == []  # unknown seeders
    assert codes("Dune.2021.2160p.WEB-DL.HEVC-FLUX", size=None) == ["quality_not_allowed"]
    assert codes("Dune.2021.CAM.x264-GRP", size=None) == ["quality_not_allowed"]
    assert codes("Dune.2021.1080p.WEB-DL.HC.x264-GRP") == ["format_score_too_low"]
    assert codes("Dune.2021.FRENCH.1080p.WEB-DL-GRP") == ["language"]


def test_all_reasons_are_reported_in_spec_order() -> None:
    d = decide(
        c("Dunes.1984.FRENCH.2160p.WEB-DL.HC-GRP", size=None, seeders=0, info_hash="x"),
        DUNE,
        ctx(blocklist=[ReleaseKey(info_hash="x")]),
    )
    assert [r.code for r in d.rejections] == [
        "blocklisted", "wrong_title", "wrong_year", "too_few_seeders", "quality_not_allowed",
        "format_score_too_low", "language",
    ]  # fmt: skip
    assert "Hardcoded subtitles -10000" in d.rejections[5].message


def test_user_rules() -> None:
    def no_yify(cand: Candidate, parsed: Any, target: Target) -> str | None:
        return "YIFY is not allowed" if parsed.group == "YIFY" else None

    assert codes("Dune.2021.1080p.WEB-DL-YIFY", cx=ctx(rules=[no_yify])) == ["rule"]
    assert codes("Dune.2021.1080p.WEB-DL-FLUX", cx=ctx(rules=[no_yify])) == []


# ---- upgrades -------------------------------------------------------------------------------


def _with(existing: Existing) -> Target:
    return Target("movie", ("Dune",), 2021, "en", existing=existing)


def test_upgrades() -> None:
    hdtv = _with(Existing("HDTV-720p"))
    assert codes("Dune.2021.1080p.WEB-DL-FLUX", hdtv) == []  # higher tier
    assert codes("Dune.2021.720p.HDTV.x264-GRP", hdtv) == ["not_an_upgrade"]  # same, same score
    assert codes("Dune.2021.720p.HDTV.Atmos.x264-GRP", hdtv) == []  # same tier, +5 score
    assert codes("Dune.2021.PROPER.720p.HDTV.x264-GRP", hdtv) == []  # same tier, proper
    br720 = _with(Existing("Bluray-720p"))
    assert codes("Dune.2021.720p.HDTV.x264-GRP", br720) == ["not_an_upgrade"]  # lower tier
    picky = ctx(profile=hd(upgrade_min_score_increment=10))
    assert codes("Dune.2021.720p.HDTV.Atmos.x264-GRP", hdtv, picky) == ["not_an_upgrade"]
    off = ctx(profile=hd(upgrade_allowed=False))
    assert codes("Dune.2021.1080p.WEB-DL-FLUX", hdtv, off) == ["upgrades_disabled"]
    done = _with(Existing("WEBDL-1080p"))
    assert codes("Dune.2021.1080p.BluRay.x264-GRP", done) == ["cutoff_met"]
    # an existing file the profile doesn't know (e.g. imported CAM) can always be replaced
    assert codes("Dune.2021.720p.HDTV.x264-GRP", _with(Existing("CAM"))) == []


# ---- ranking --------------------------------------------------------------------------------


def _ranked(cands: list[Candidate], target: Target = DUNE) -> list[str]:
    return [d.candidate.title for d in decide_all(cands, target, ctx())]


def test_rank_order() -> None:
    order = _ranked(
        [
            c("Dune.2021.720p.HDTV.x264-A", seeders=900),
            c("Dune.2021.1080p.BluRay.x264-REJ", seeders=0),  # rejected: no seeders
            c("Dune.2021.1080p.WEB-DL-B", seeders=50),
            c("Dune.2021.1080p.WEB-DL.Atmos-C", seeders=5),  # +5 score beats seeders
            c("Dune.2021.1080p.BluRay.x264-D", seeders=10),  # higher tier wins outright
            c("Dune.2021.REPACK.1080p.WEB-DL-E", seeders=1),  # revision beats format score
        ]
    )
    assert order == [
        "Dune.2021.1080p.BluRay.x264-D",
        "Dune.2021.REPACK.1080p.WEB-DL-E",
        "Dune.2021.1080p.WEB-DL.Atmos-C",
        "Dune.2021.1080p.WEB-DL-B",
        "Dune.2021.720p.HDTV.x264-A",
        "Dune.2021.1080p.BluRay.x264-REJ",
    ]


def test_rank_seeders_bucket_then_priority_then_preferred_size() -> None:
    # 40 and 60 seeders are the same order of magnitude, so indexer priority decides.
    order = _ranked([c("Dune.2021.1080p.WEB-DL-A", seeders=60, priority=30),
                     c("Dune.2021.1080p.WEB-DL-B", seeders=40, priority=10),
                     c("Dune.2021.1080p.WEB-DL-C", seeders=900, priority=50)])  # fmt: skip
    assert order == ["Dune.2021.1080p.WEB-DL-C", "Dune.2021.1080p.WEB-DL-B",
                     "Dune.2021.1080p.WEB-DL-A"]  # fmt: skip

    pref = DecisionContext(profile=hd(sizes={"WEBDL-1080p": {"min": 5, "preferred": 30}}))
    near, far = (
        c("Dune.2021.1080p.WEB-DL-NEAR", size=4.6 * GB),
        c("Dune.2021.1080p.WEB-DL-FAR", size=12 * GB),
    )
    ds = decide_all([far, near], DUNE, pref)
    assert [d.candidate.title for d in ds] == [near.title, far.title]


def test_select_movie() -> None:
    ds = decide_all([c("Dune.2021.CAM-X"), c("Dune.2021.1080p.WEB-DL-B")], DUNE, ctx())
    assert [d.candidate.title for d in select(ds, DUNE)] == ["Dune.2021.1080p.WEB-DL-B"]
    assert select(decide_all([c("Dune.2021.CAM-X")], DUNE, ctx()), DUNE) == []


# ---- series ---------------------------------------------------------------------------------


def show(
    wanted: list[tuple[int, int]],
    seasons: dict[int, SeasonInfo] | None = None,
    **kw: Any,
) -> Target:
    return Target(
        "series",
        ("Severance",),
        2022,
        "en",
        runtime_minutes=50,
        episodes=tuple(WantedEpisode(s, e) for s, e in wanted),
        seasons=seasons
        if seasons is not None
        else {1: SeasonInfo(9, True), 2: SeasonInfo(10, False)},
        **kw,
    )


S1_ALL = [(1, e) for e in range(1, 10)]


def test_episode_matching() -> None:
    t = show([(1, 3), (1, 4)])
    d = decide(c("Severance.S01E03.1080p.WEB-DL-GRP", size=None), t, ctx())
    assert d.accepted and d.episodes == [(1, 3)] and not d.season_pack
    d = decide(c("Severance.S01E03E04.1080p.WEB-DL-GRP", size=None), t, ctx())
    assert d.episodes == [(1, 3), (1, 4)]
    assert codes("Severance.S01E05.1080p.WEB-DL-GRP", t, size=None) == ["wrong_episode"]
    assert codes("Severance.2022.1080p.WEB-DL-GRP", t, size=None) == ["wrong_media_type"]


def test_daily_and_absolute_matching() -> None:
    daily = Target("series", ("The Daily Show",), episodes=(
        WantedEpisode(28, 50, air_date=date(2023, 5, 4)),))  # fmt: skip
    d = decide(c("The.Daily.Show.2023.05.04.1080p.WEB.h264-GRP", size=None), daily, ctx())
    assert d.accepted and d.episodes == [(28, 50)]
    anime = Target("series", ("One Piece",), episodes=(WantedEpisode(21, 79, absolute=1071),))
    d = decide(c("[GRP] One Piece - 1071 [1080p] WEB-DL", size=None), anime, ctx())
    assert d.episodes == [(21, 79)] and d.accepted, d.rejections


def test_season_pack_rule() -> None:
    pack = "Severance.S01.1080p.WEB-DL-GRP"
    d = decide(c(pack, size=None), show(S1_ALL), ctx())
    assert d.accepted and d.season_pack and len(d.episodes) == 9
    # only part of the season is missing: the pack would re-download the rest
    assert codes(pack, show([(1, 1), (1, 2)]), size=None) == ["season_pack_not_wanted"]
    assert codes(pack, show([(1, 1), (1, 2)]), ctx(season_pack_share=0.2), size=None) == []
    # season still airing
    s2 = [(2, e) for e in range(1, 11)]
    assert codes("Severance.S02.1080p.WEB-DL-GRP", show(s2), size=None) == ["season_not_finished"]


def test_pack_size_is_per_episode() -> None:
    # 9 episodes x 50 min at 4 GB each = 36 GB: fine for a pack, far too big for one episode.
    assert codes("Severance.S01.1080p.WEB-DL-GRP", show(S1_ALL), size=36 * GB) == []
    assert codes("Severance.S01E01.1080p.WEB-DL-GRP", show(S1_ALL), size=36 * GB) == [
        "size_too_large"
    ]


def test_select_prefers_packs_then_fills_gaps() -> None:
    t = show([*S1_ALL, (2, 1), (2, 2)])
    ds = decide_all(
        [
            c("Severance.S01E01.1080p.BluRay.x264-BEST", size=None),  # better, but pack wins
            c("Severance.S01.1080p.WEB-DL-PACK", size=None),
            c("Severance.S01.720p.HDTV.x264-WORSEPACK", size=None),
            c("Severance.S02E01.1080p.WEB-DL-A", size=None),
            c("Severance.S02E01E02.720p.HDTV.x264-B", size=None),  # overlaps the pick for E01
            c("Severance.S02E02.720p.HDTV.x264-C", size=None),
        ],
        t,
        ctx(),
    )
    assert [d.candidate.title for d in select(ds, t)] == [
        "Severance.S01.1080p.WEB-DL-PACK",
        "Severance.S02E01.1080p.WEB-DL-A",
        "Severance.S02E02.720p.HDTV.x264-C",
    ]


def test_series_upgrade_must_beat_every_covered_file() -> None:
    t = Target(
        "series", ("Severance",), runtime_minutes=50,
        episodes=(WantedEpisode(1, 1, existing=Existing("HDTV-720p")),
                  WantedEpisode(1, 2, existing=Existing("Bluray-720p"))),
        seasons={1: SeasonInfo(2, True)},
    )  # fmt: skip
    assert codes("Severance.S01E01.720p.BluRay.x264-GRP", t, size=None) == []
    assert codes("Severance.S01.720p.BluRay.x264-GRP", t, size=None) == ["not_an_upgrade"]


def test_target_needs_a_name() -> None:
    with pytest.raises(ValueError, match="name"):
        Target("movie", ())


# ---- search fan-out -------------------------------------------------------------------------


class FakeSource:
    def __init__(self, id: str, results: list[Candidate] | Exception, *, delay: float = 0,
                 priority: int = 25) -> None:  # fmt: skip
        self.id = self.name = id
        self.priority = priority
        self.results = results
        self.delay = delay
        self.queries: list[SearchQuery] = []

    async def search(self, query: SearchQuery) -> list[Candidate]:
        self.queries.append(query)
        await asyncio.sleep(self.delay)
        if isinstance(self.results, Exception):
            raise self.results
        return self.results


async def test_search_fans_out_and_survives_failing_sources() -> None:
    good = FakeSource("good", [c("Dune.2021.1080p.WEB-DL-FLUX", info_hash="AA", seeders=10)])
    dup = FakeSource("dup", [c("Dune.2021.1080p.WEB-DL-FLUX", info_hash="aa", seeders=99),
                             c("Dune.2021.720p.HDTV.x264-GRP")], priority=5)  # fmt: skip
    broken = FakeSource("broken", RuntimeError("HTTP 500"))
    slow = FakeSource("slow", [c("Dune.2021.2160p.WEB-DL-X")], delay=5)
    q = SearchQuery("movie", ("Dune",), 2021)
    out = await search([good, dup, broken, slow], q, DUNE, ctx(), source_timeout=0.2)

    assert {(f.source_id, f.error) for f in out.failures} == {
        ("broken", "HTTP 500"), ("slow", "timed out after 0.2s"),
    }  # fmt: skip
    titles = [(d.candidate.title, d.candidate.source_id) for d in out.decisions]
    # the same hash from two indexers is listed once, from the higher-priority one
    assert titles == [
        ("Dune.2021.1080p.WEB-DL-FLUX", "dup"),
        ("Dune.2021.720p.HDTV.x264-GRP", "dup"),
    ]
    assert out.decisions[0].candidate.priority == 5
    assert [d.candidate.title for d in out.picks] == ["Dune.2021.1080p.WEB-DL-FLUX"]
    assert good.queries == [q]


def test_decision_serializes() -> None:
    d = decide(c("Dune.2021.1080p.WEB-DL-FLUX", info_hash="aa"), DUNE, ctx())
    data = d.model_dump(mode="json")
    assert data["accepted"] is True and data["candidate"]["info_hash"] == "aa"
    assert data["parsed"]["group"] == "FLUX"


def test_movie_target_from_title() -> None:
    from fetchbox.modules.media.models import Title
    from fetchbox.modules.releases.targets import movie_target

    t = Title(id="t1", kind="movie", name="Amélie", year=2001, meta={
        "original_name": "Le Fabuleux Destin d'Amélie Poulain", "original_language": "fr",
        "runtime": 122, "alt_titles": ["Amelie", "Amélie"]})  # fmt: skip
    target = movie_target(t)
    assert target.names == ("Amélie", "Le Fabuleux Destin d'Amélie Poulain", "Amelie")
    assert target.original_language == "fr" and target.runtime_minutes == 122
    french = c("Le.Fabuleux.Destin.d.Amelie.Poulain.2001.FRENCH.1080p.BluRay.x264-GRP", size=None)
    assert decide(french, target, ctx()).accepted  # original title, original language
    with pytest.raises(ValueError, match="not a movie"):
        movie_target(Title(id="t2", kind="series", name="X"))
