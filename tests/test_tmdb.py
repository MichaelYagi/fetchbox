"""TMDB provider against a fake TMDB."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import SecretStr

from fetchbox.core.providers import registry
from fetchbox.providers.metadata import MetadataError
from fetchbox.providers.metadata.tmdb import TmdbProvider, TmdbSettings
from tests.fake_tmdb import V3_KEY, FakeTmdb


def provider(fake: FakeTmdb, key: str = V3_KEY) -> TmdbProvider:
    return TmdbProvider(TmdbSettings(api_key=SecretStr(key)), transport=fake.transport)


async def test_search_and_movie_detail() -> None:
    fake = FakeTmdb()
    p = provider(fake)
    hits = await p.search("dune", "movie", 2021)
    assert [(h.tmdb_id, h.name, h.year) for h in hits] == [(438631, "Dune", 2021)]
    assert hits[0].poster_url and hits[0].poster_url.startswith("https://image.tmdb.org/")
    m = await p.get_movie("438631")
    assert m.imdb_id == "tt1160419" and m.runtime == 155 and m.original_language == "en"
    assert m.release_date == date(2021, 9, 15) and m.genres == ["Science Fiction"]
    # alternate titles: deduplicated, the primary name left out
    assert m.alt_titles == ["Dune: Part One", "Dune : Première partie"]
    sent = fake.requests[-1].url.params
    assert sent["api_key"] == V3_KEY and sent["language"] == "en-US"
    await p.aclose()


async def test_v4_token_is_sent_as_bearer() -> None:
    token = "eyJhbGciOiJIUzI1NiJ9.fake.token"
    fake = FakeTmdb(key=token)
    p = provider(fake, token)
    assert (await p.test()).ok
    req = fake.requests[-1]
    assert req.headers["authorization"] == f"Bearer {token}"
    assert "api_key" not in req.url.params


async def test_errors() -> None:
    fake = FakeTmdb()
    bad = provider(fake, "wrong")
    with pytest.raises(MetadataError) as exc:
        await bad.get_movie("438631")
    assert exc.value.kind == "auth"
    res = await bad.test()
    assert not res.ok and "rejected" in res.message

    p = provider(fake)
    with pytest.raises(MetadataError) as exc:
        await p.get_movie("1")
    assert exc.value.kind == "not_found"

    fake.fail_next = [429]  # retried once after Retry-After
    assert (await p.get_movie("438631")).name == "Dune"
    fake.fail_next = [429, 429]
    with pytest.raises(MetadataError) as exc:
        await p.get_movie("438631")
    assert exc.value.kind == "rate_limited"
    fake.fail_next = [500]
    with pytest.raises(MetadataError) as exc:
        await p.get_movie("438631")
    assert exc.value.kind == "unavailable"


async def test_series_with_seasons() -> None:
    p = provider(FakeTmdb())
    s = await p.get_series("95396")
    assert s.tvdb_id == 371980 and s.year == 2022 and not s.ended
    assert [(x.number, x.episode_count) for x in s.seasons] == [(1, 9), (2, 10)]
    assert s.seasons[0].episodes[0].name == "Ep 1"
    assert s.alt_titles == []  # only the primary name, which is excluded


async def test_find_by_external_id() -> None:
    p = provider(FakeTmdb())
    hit = await p.find_by_external_id("imdb", "tt1160419")
    assert hit is not None and hit.kind == "movie" and hit.tmdb_id == 438631
    assert await p.find_by_external_id("imdb", "tt0000001") is None
    with pytest.raises(MetadataError):
        await p.find_by_external_id("imdb", "../movie/1")  # never reaches the URL


def test_registered() -> None:
    assert registry.get("metadata", "tmdb") is TmdbProvider
    schema = registry.settings_schema("metadata", "tmdb")
    assert schema["properties"]["api_key"]["format"] == "password"
