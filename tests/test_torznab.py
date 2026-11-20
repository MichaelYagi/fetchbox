"""Torznab provider against a fake endpoint."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from fetchbox.providers.indexers import IndexerError, ReleaseCandidate, SearchQuery
from fetchbox.providers.indexers.torznab import (
    TorznabProvider,
    TorznabSettings,
    info_hash_from_magnet,
)
from tests.fake_torznab import API_KEY, GB, FakeTorznab, Item

H = "a" * 40


def provider(fake: FakeTorznab, **kw: object) -> TorznabProvider:
    settings = TorznabSettings.model_validate({"url": "http://fake/", "api_key": API_KEY, **kw})
    return TorznabProvider(settings, transport=fake.transport)


DUNE = SearchQuery("movie", ("Dune",), 2021, ids={"imdb": "tt1160419", "tmdb": "438631"})


async def test_movie_search_by_imdb_and_item_parsing() -> None:
    fake = FakeTorznab([
        Item("Dune.2021.1080p.WEB-DL-FLUX", infohash=H.upper(), down=0, up=2),
        Item("Dune.2021.720p.HDTV-GRP", seeders=None, peers=None, size=None,
             magnet="magnet:?xt=urn:btih:" + "b" * 40 + "&dn=x", extra={"tag": "internal"}),
    ])  # fmt: skip
    found = await provider(fake).search(DUNE)
    params = fake.last("movie")
    assert params["imdbid"] == "1160419" and params["cat"] == "2000" and params["extended"] == "1"
    assert params["limit"] == "100" and "q" not in params
    a, b = found
    assert a.title == "Dune.2021.1080p.WEB-DL-FLUX" and a.info_hash == H
    assert a.size == 4 * GB and a.seeders == 50 and a.leechers == 10
    assert a.indexer_flags == {"freeleech", "doubleupload"} and a.categories == (2040,)
    assert a.published_at == datetime(2026, 11, 18, 10, tzinfo=UTC)
    assert a.guid == "http://fake/details/0" and a.download_url and a.magnet_url is None
    assert b.info_hash == "b" * 40 and b.size is None and b.seeders is None
    assert b.indexer_flags == {"internal"}


async def test_falls_back_to_text_search() -> None:
    fake = FakeTorznab([Item("Dune.2021.1080p.WEB-DL-FLUX")])
    fake.by_id_finds_nothing = True
    found = await provider(fake).search(DUNE)
    assert len(found) == 1
    assert fake.last("movie")["q"] == "Dune 2021"  # caps don't list "year": put it in q


async def test_tv_queries() -> None:
    fake = FakeTorznab([Item("Show.S01E02.1080p.WEB-DL-GRP", category=5000)])
    p = provider(fake)
    await p.search(SearchQuery("series", ("Show",), season=1, episode=2, ids={"tvdb": "123"}))
    want = {"tvdbid": "123", "season": "1", "ep": "2", "cat": "5000"}
    assert want.items() <= fake.last("tvsearch").items()
    await p.search(SearchQuery("series", ("Daily",), air_date=date(2023, 5, 4)))
    assert fake.last("tvsearch")["season"] == "2023" and fake.last("tvsearch")["ep"] == "05/04"


@pytest.mark.parametrize(
    ("setup", "kind"),
    [
        ({"key": "wrong"}, "auth"),
        ({"fail": 429}, "rate_limited"),
        ({"fail": 503}, "unavailable"),
        ({"error_doc": ("500", "Request limit reached")}, "rate_limited"),
        ({"error_doc": ("201", "Incorrect parameter")}, "unavailable"),
    ],
)
async def test_errors(setup: dict[str, object], kind: str) -> None:
    fake = FakeTorznab()
    for k, v in setup.items():
        setattr(fake, k, v)
    with pytest.raises(IndexerError) as exc:
        await provider(fake).search(DUNE)
    assert exc.value.kind == kind
    if setup.get("fail") == 429:
        assert exc.value.retry_after == 120


async def test_hostile_xml_is_refused() -> None:
    import httpx

    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><caps>&a;</caps>'
    p = TorznabProvider(
        TorznabSettings(url="http://fake"),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=bomb)),
    )
    with pytest.raises(IndexerError, match="DTD"):
        await p.caps()


async def test_test_reports_capabilities() -> None:
    res = await provider(FakeTorznab()).test()
    assert res.ok and res.details["movie_search"] == ["imdbid", "q"]
    bad = await provider(FakeTorznab(key="other")).test()
    assert not bad.ok and bad.details["kind"] == "auth"


async def test_resolve() -> None:
    p = provider(FakeTorznab())
    magnet = "magnet:?xt=urn:btih:" + H
    assert (await p.resolve(ReleaseCandidate(title="x", magnet_url=magnet))).magnet == magnet
    got = await p.resolve(ReleaseCandidate(title="x", download_url="http://fake/dl/1"))
    assert got.torrent == b"d8:announce3:urle"
    redirected = await p.resolve(
        ReleaseCandidate(title="x", download_url="http://fake/dl/redirect")
    )
    assert redirected.magnet and redirected.magnet.endswith("c" * 40)
    with pytest.raises(IndexerError):
        await p.resolve(ReleaseCandidate(title="x"))


def test_magnet_hash_forms() -> None:
    assert info_hash_from_magnet("magnet:?xt=urn:btih:" + H.upper()) == H
    b32 = "MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43UOV3HO6DZPJQWEY3EMVTGO2DJ"[:32]
    assert info_hash_from_magnet("magnet:?xt=urn:btih:" + b32) is not None
    assert info_hash_from_magnet("magnet:?dn=nohash") is None


def test_settings_validation() -> None:
    with pytest.raises(ValidationError):
        TorznabSettings(url="ftp://x")
    assert TorznabSettings(url="http://x/api/").url == "http://x/api"
