"""RSS sync: new releases in indexer feeds are matched to wanted items and grabbed.

The Phase 2 gate: a series monitored for future episodes only grabs a new episode from
the feed, and nothing it doesn't want.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from fetchbox.core.db import utcnow
from fetchbox.modules.automation.rss import rss_sync
from fetchbox.modules.media.models import Episode
from tests.conftest import Running
from tests.fake_torznab import Item
from tests.pipeline import Fakes, _indexer, _movie, standard_hd
from tests.torrents import make_torrent

NEW_EP = "Severance.S02E10.1080p.WEB-DL.DDP5.1.H.264-NTb"
OLD_EP = "Severance.S01E02.1080p.WEB-DL.DDP5.1.H.264-NTb"
MOVIE = "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"
OTHER_MOVIE = "Dune.Part.Two.2024.1080p.WEB-DL-GRP"
UNKNOWN = "Some.Other.Show.S01E01.1080p.WEB-DL-GRP"


@pytest.fixture(autouse=True)
def slow_sweep(server: Running) -> None:
    server.app.state.downloads.poll_interval = 0.2
    server.app.state.autosearch.sweep_interval = 3600  # only RSS grabs in these tests


@pytest.fixture
def feed(tmp_path: Path) -> list[Item]:
    out = []
    for name in (NEW_EP, OLD_EP, MOVIE, OTHER_MOVIE, UNKNOWN):
        t = make_torrent(tmp_path / name, name, {"x.mkv": 1000})
        cat = 5040 if ".S0" in name else 2040
        out.append(Item(name, size=3_000_000_000, magnet=t.magnet, infohash=t.info_hash,
                        category=cat))  # fmt: skip
    return out


async def _grabs(c: httpx.AsyncClient, wanted_item_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = (
        await c.get("/grabs", params={"wanted_item_id": wanted_item_id})
    ).json()
    return out


async def test_future_only_series_grabs_the_new_episode(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, feed: list[Item]
) -> None:
    tomorrow = (utcnow() + timedelta(days=1)).date().isoformat()
    fakes.tmdb.seasons[(95396, 2)][-1]["air_date"] = tomorrow  # S02E10 not aired yet
    await _indexer(c, fakes, "one", feed)
    body = {"kind": "series", "tmdb_id": 95396, "monitor_mode": "future",
            "profile_id": await standard_hd(c), "search": False}  # fmt: skip
    t = (await c.post("/titles", json=body)).json()
    seasons = (await c.get(f"/titles/{t['id']}/episodes")).json()
    eps = {(s["number"], e["number"]): e for s in seasons for e in s["episodes"]}
    assert eps[(2, 10)]["wanted"]["monitored"] and not eps[(1, 2)]["wanted"]["monitored"]

    # before it airs, the feed's S02E10 is ignored
    out = await rss_sync(server.app.state.autosearch)
    assert out["grabbed"] == [] and out["releases"] == len(feed) and out["indexers"] == 1

    # it aired: the next sync grabs it, and only it
    async with server.app.state.fb.db.sessionmaker() as s:
        await s.execute(
            update(Episode).where(Episode.id == eps[(2, 10)]["id"]).values(air_date=utcnow().date())
        )
        await s.commit()
    out = await rss_sync(server.app.state.autosearch)
    assert out["grabbed"] == [NEW_EP], out
    [g] = await _grabs(c, eps[(2, 10)]["wanted"]["id"])
    assert g["release_title"] == NEW_EP and g["reason"] == "rss"
    assert await _grabs(c, eps[(1, 2)]["wanted"]["id"]) == []  # not monitored
    seasons = (await c.get(f"/titles/{t['id']}/episodes")).json()
    w = next(e for x in seasons for e in x["episodes"] if e["id"] == eps[(2, 10)]["id"])["wanted"]
    assert w["state"] in ("grabbed", "downloading") and w["last_search_at"] is None

    # the same feed again: nothing new to do
    assert (await rss_sync(server.app.state.autosearch))["grabbed"] == []


async def test_missing_movie_grabbed_from_the_feed(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, feed: list[Item]
) -> None:
    await _indexer(c, fakes, "one", feed)
    movie = await _movie(c, search=False)
    wid = movie["wanted"][0]["id"]
    before = movie["wanted"][0]["next_search_at"]
    out = await rss_sync(server.app.state.autosearch)
    assert out["grabbed"] == [MOVIE], out  # not Dune Part Two (another title)
    [g] = await _grabs(c, wid)
    assert g["reason"] == "rss"
    after = (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]
    assert after["next_search_at"] == before and after["state"] in ("grabbed", "downloading")


async def test_unreleased_movie_and_failing_feed(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, feed: list[Item]
) -> None:
    soon = (utcnow() + timedelta(days=30)).date().isoformat()
    fakes.tmdb.movies[438631]["release_dates"] = {
        "results": [{"iso_3166_1": "US", "release_dates": [
            {"type": 4, "release_date": f"{soon}T00:00:00.000Z"}]}]
    }  # fmt: skip
    await _indexer(c, fakes, "one", feed)
    await _indexer(c, fakes, "broken", [])
    fakes.torznab["broken"].fail = 500
    movie = await _movie(c, search=False)
    out = await rss_sync(server.app.state.autosearch)
    assert out["grabbed"] == [] and out["failed_indexers"] == ["broken"], out
    assert await _grabs(c, movie["wanted"][0]["id"]) == []  # waits for its release
