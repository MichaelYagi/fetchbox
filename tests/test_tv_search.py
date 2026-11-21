"""TV search: episode and season searches, season packs first, aired episodes only."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import Running
from tests.fake_torznab import Item
from tests.pipeline import Fakes, _indexer, standard_hd
from tests.torrents import make_torrent, until

PACK = "Severance.S01.1080p.WEB-DL.DDP5.1.H.264-NTb"
S01E02 = "Severance.S01E02.1080p.WEB-DL.DDP5.1.H.264-NTb"
S02E01 = "Severance.S02E01.1080p.WEB-DL.DDP5.1.H.264-NTb"
S02E02 = "Severance.S02E02.1080p.WEB-DL.DDP5.1.H.264-NTb"


@pytest.fixture(autouse=True)
def fast(server: Running) -> None:
    server.app.state.autosearch.sweep_interval = 0.2
    server.app.state.downloads.poll_interval = 0.2


@pytest.fixture
def items(tmp_path: Path) -> list[Item]:
    """Real (unseeded) torrents, so grabs become downloads that just wait."""
    out = []
    for name, size in ((PACK, 30_000_000_000), (S01E02, 3_000_000_000),
                       (S02E01, 3_000_000_000), (S02E02, 3_000_000_000)):  # fmt: skip
        t = make_torrent(tmp_path / name, name, {"x.mkv": 1000})
        out.append(Item(name, size=size, magnet=t.magnet, infohash=t.info_hash, category=5040))
    return out


async def _series(c: httpx.AsyncClient, *, search: bool) -> dict[str, Any]:
    r = await c.post(
        "/titles",
        json={"kind": "series", "tmdb_id": 95396, "profile_id": await standard_hd(c),
              "monitor_mode": "all", "search": search},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


async def _episodes(c: httpx.AsyncClient, title_id: str) -> dict[tuple[int, int], dict[str, Any]]:
    seasons = (await c.get(f"/titles/{title_id}/episodes")).json()
    return {(s["number"], e["number"]): e for s in seasons for e in s["episodes"]}


async def test_interactive_episode_and_season(
    c: httpx.AsyncClient, fakes: Fakes, items: list[Item]
) -> None:
    await _indexer(c, fakes, "one", items)
    t = await _series(c, search=False)
    eps = await _episodes(c, t["id"])

    r = await c.get("/releases", params={"wanted_item_id": eps[(1, 2)]["wanted"]["id"]})
    assert r.status_code == 200, r.text
    res = {x["candidate"]["title"]: x for x in r.json()["releases"]}
    assert res[S01E02]["accepted"] and res[S01E02]["episodes"] == [[1, 2]]
    assert not res[PACK]["accepted"]  # a 9-episode pack for one wanted episode
    assert not res[S02E01]["accepted"]
    q = fakes.torznab["one"].last("tvsearch")
    assert q["season"] == "1" and q["ep"] == "2"

    r = await c.get("/releases", params={"title_id": t["id"], "season": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["season"] == 1 and body["wanted_item_id"] is None
    res = {x["candidate"]["title"]: x for x in body["releases"]}
    assert res[PACK]["accepted"] and res[PACK]["season_pack"]
    assert body["picks"] == [res[PACK]["release_id"]]  # the pack covers it all
    assert "ep" not in fakes.torznab["one"].last("tvsearch")  # asked for the season

    # grabbing the pack claims every episode it covers
    r = await c.post("/grabs", json={"release_id": res[PACK]["release_id"]})
    assert r.status_code == 201, r.text
    eps = await _episodes(c, t["id"])
    assert {eps[(1, n)]["wanted"]["state"] for n in range(1, 10)} <= {"grabbed", "downloading"}
    assert eps[(2, 1)]["wanted"]["state"] == "missing"

    assert (await c.get("/releases", params={"title_id": t["id"]})).status_code == 422
    assert (await c.get("/releases", params={"title_id": t["id"], "season": 7})).status_code == 404


async def test_automatic_search_packs_first_then_episodes(
    c: httpx.AsyncClient, fakes: Fakes, items: list[Item]
) -> None:
    soon = (date.today() + timedelta(days=20)).isoformat()
    fakes.tmdb.seasons[(95396, 2)][-1]["air_date"] = soon  # S02E10 hasn't aired
    await _indexer(c, fakes, "one", items)
    t = await _series(c, search=True)

    async def settled() -> bool:
        eps = await _episodes(c, t["id"])
        return all(
            e["wanted"]["next_search_at"] is not None and e["wanted"]["state"] != "searching"
            for (s, n), e in eps.items()
            if (s, n) != (2, 10)
        )

    await until(settled, within=30)
    eps = await _episodes(c, t["id"])
    busy = {"grabbed", "downloading"}
    states = {k: e["wanted"]["state"] for k, e in eps.items()}
    assert all(states[(1, n)] in busy for n in range(1, 10)), states  # the pack
    assert eps[(2, 1)]["wanted"]["state"] in busy and eps[(2, 2)]["wanted"]["state"] in busy
    assert all(eps[(2, n)]["wanted"]["state"] == "missing" for n in range(3, 11))
    assert eps[(2, 10)]["wanted"]["last_search_at"] is None  # not aired: not searched

    grabs: dict[str, int] = {}
    for key in ((1, 1), (2, 1), (2, 2)):
        for g in (
            await c.get("/grabs", params={"wanted_item_id": eps[key]["wanted"]["id"]})
        ).json():
            grabs[g["release_title"]] = len(g["items"]) if "items" in g else 0
    assert set(grabs) == {PACK, S02E01, S02E02}
    pack = (await c.get("/grabs", params={"wanted_item_id": eps[(1, 5)]["wanted"]["id"]})).json()
    assert [g["release_title"] for g in pack] == [PACK]

    # searching again: nothing left that can be found
    r = await c.post(f"/titles/{t['id']}/search")
    assert r.status_code == 202, r.text  # season 2 still has aired missing episodes


async def test_title_search_conflicts_when_nothing_to_get(
    c: httpx.AsyncClient, fakes: Fakes
) -> None:
    t = await _series(c, search=False)
    r = await c.post(f"/titles/{t['id']}/search")
    assert r.status_code == 202  # aired missing episodes in both seasons
    r = await c.post(f"/titles/{t['id']}/search")
    assert r.status_code == 409  # both seasons' searches are already queued


async def test_search_one_season_now(c: httpx.AsyncClient, fakes: Fakes, items: list[Item]) -> None:
    await _indexer(c, fakes, "one", items)
    t = await _series(c, search=False)
    r = await c.post(f"/titles/{t['id']}/seasons/1/search")
    assert r.status_code == 202, r.text
    assert (await c.post(f"/titles/{t['id']}/seasons/1/search")).status_code in (202, 409)
    assert (await c.post(f"/titles/{t['id']}/seasons/9/search")).status_code == 404

    async def grabbed() -> bool:
        eps = await _episodes(c, t["id"])
        return eps[(1, 1)]["wanted"]["state"] in ("grabbed", "downloading")

    await until(grabbed, within=20)
