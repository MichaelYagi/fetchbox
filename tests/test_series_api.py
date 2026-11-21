"""Series: adding with seasons and episodes, the six monitor modes, refresh, seasons."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from fetchbox.modules.media.series import Ep, for_new, initial, seasons_monitored
from tests.conftest import Running
from tests.fake_tmdb import FakeTmdb
from tests.pipeline import Fakes

TODAY = date(2026, 6, 1)


def _eps() -> list[Ep]:
    out = [Ep(0, 1, date(2020, 1, 1))]  # a special
    out += [Ep(1, n, date(2022, 2, n)) for n in range(1, 4)]
    out += [Ep(2, 1, date(2026, 5, 1)), Ep(2, 2, date(2026, 7, 1)), Ep(2, 3, None)]
    return out


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("all", {(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)}),
        ("future", {(2, 2), (2, 3)}),  # not aired yet (no date counts as not aired)
        ("missing", {(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)}),
        ("first_season", {(1, 1), (1, 2), (1, 3)}),
        ("latest_season", {(2, 1), (2, 2), (2, 3)}),
        ("none", set()),
    ],
)
def test_initial_monitoring(mode: Any, expected: set[tuple[int, int]]) -> None:
    flags = initial(mode, _eps(), TODAY)
    assert {k for k, on in flags.items() if on} == expected
    assert flags[(0, 1)] is False  # specials never by mode


def test_missing_mode_skips_episodes_with_files() -> None:
    eps = [Ep(1, 1, date(2022, 1, 1), has_file=True), Ep(1, 2, date(2022, 1, 8))]
    assert initial("missing", eps, TODAY) == {(1, 1): False, (1, 2): True}


def test_new_episodes() -> None:
    # a new episode in a known, monitored season
    assert for_new("all", 2, True, 2) and for_new("future", 2, True, 2)
    assert not for_new("all", 2, False, 2)  # the user unmonitored that season
    assert not for_new("none", 2, None, 2)
    # a new season
    assert for_new("future", 3, None, 3) and for_new("missing", 3, None, 3)
    assert not for_new("first_season", 3, None, 3) and for_new("first_season", 1, True, 3)
    assert for_new("latest_season", 3, None, 3)
    assert not for_new("latest_season", 2, False, 3)
    assert not for_new("all", 0, None, 3)  # specials stay off ...
    assert for_new("all", 0, True, 3)  # ... unless the user monitored them
    flags = initial("first_season", _eps(), TODAY)
    assert seasons_monitored("first_season", flags) == {0: False, 1: True, 2: False}
    assert seasons_monitored("future", initial("future", _eps(), TODAY))[1] is True


async def _hd(c: httpx.AsyncClient) -> str:
    profiles = (await c.get("/quality-profiles")).json()
    return str(next(p["id"] for p in profiles if p["name"] == "1080p"))


async def _add(c: httpx.AsyncClient, mode: str = "all") -> dict[str, Any]:
    r = await c.post(
        "/titles",
        json={"kind": "series", "tmdb_id": 95396, "profile_id": await _hd(c),
              "monitor_mode": mode, "search": False},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def _monitored(seasons: list[dict[str, Any]]) -> set[tuple[int, int]]:
    return {
        (s["number"], e["number"])
        for s in seasons
        for e in s["episodes"]
        if e["wanted"]["monitored"]
    }


async def test_add_series_with_episodes(c: httpx.AsyncClient, fakes: Fakes) -> None:
    fakes.tmdb.seasons[(95396, 0)] = [{"episode_number": 1, "name": "Special", "air_date": None}]
    t = await _add(c)
    assert t["kind"] == "series" and t["monitor_mode"] == "all" and t["tvdb_id"] == 371980
    assert t["wanted"] == []
    assert t["episodes"] == {"episodes": 19, "monitored": 19, "acquired": 0, "missing": 19}

    seasons = (await c.get(f"/titles/{t['id']}/episodes")).json()
    assert [(s["number"], s["monitored"], len(s["episodes"])) for s in seasons] == [
        (0, False, 1), (1, True, 9), (2, True, 10),
    ]  # fmt: skip
    ep = seasons[1]["episodes"][0]
    assert ep["name"] == "Ep 1" and ep["air_date"] == "2022-01-11"
    assert ep["wanted"]["state"] == "missing" and ep["wanted"]["episode_id"] == ep["id"]

    # wanted rows say which episode they are
    rows = (await c.get("/wanted", params={"limit": 500})).json()["items"]
    assert len(rows) == 20 and all(r["title_kind"] == "series" for r in rows)
    s1e1 = next(r for r in rows if (r["season_number"], r["episode_number"]) == (1, 1))
    assert s1e1["episode_name"] == "Ep 1" and s1e1["air_date"] == "2022-01-11"

    again = {"kind": "series", "tmdb_id": 95396, "profile_id": await _hd(c)}
    assert (await c.post("/titles", json=again)).status_code == 409
    assert (await c.get(f"/titles/{t['id']}/episodes")).status_code == 200


async def test_modes_seasons_and_refresh(c: httpx.AsyncClient, fakes: Fakes) -> None:
    soon = (date.today() + timedelta(days=30)).isoformat()
    fakes.tmdb.seasons[(95396, 2)][-1]["air_date"] = soon  # S02E10 airs next month
    t = await _add(c, "future")
    seasons = (await c.get(f"/titles/{t['id']}/episodes")).json()
    assert _monitored(seasons) == {(2, 10)}
    assert t["episodes"]["missing"] == 0  # nothing monitored has aired

    # switching mode re-applies it to every episode
    r = await c.patch(f"/titles/{t['id']}", json={"monitor_mode": "first_season"},
                      headers={"If-Match": t["etag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    seasons = (await c.get(f"/titles/{t['id']}/episodes")).json()
    assert _monitored(seasons) == {(1, n) for n in range(1, 10)}
    r = await c.patch(f"/titles/{t['id']}", json={"monitor_mode": "latest_season"})
    assert _monitored((await c.get(f"/titles/{t['id']}/episodes")).json()) == {
        (2, n) for n in range(1, 11)
    }
    assert (await c.patch(f"/titles/{t['id']}", json={"monitor_mode": "bogus"})).status_code == 422

    # a whole season off, then on
    r = await c.patch(f"/titles/{t['id']}/seasons/2", json={"monitored": False})
    assert r.status_code == 200 and r.json()["episodes"]["monitored"] == 0
    r = await c.patch(f"/titles/{t['id']}/seasons/2", json={"monitored": True})
    assert r.json()["episodes"]["monitored"] == 10
    assert (
        await c.patch(f"/titles/{t['id']}/seasons/9", json={"monitored": True})
    ).status_code == 404

    # refresh: a new episode in season 2 and a new season 3; latest_season moves on
    fakes.tmdb.seasons[(95396, 2)].append({"episode_number": 11, "name": "Ep 11", "air_date": soon})
    fakes.tmdb.seasons[(95396, 3)] = [{"episode_number": 1, "name": "New", "air_date": None}]
    r = await c.post(f"/titles/{t['id']}/refresh")
    assert r.status_code == 200, r.text
    seasons = (await c.get(f"/titles/{t['id']}/episodes")).json()
    assert [s["number"] for s in seasons] == [1, 2, 3]
    mon = _monitored(seasons)
    assert (2, 11) in mon and (3, 1) in mon and (1, 1) not in mon
    assert r.json()["episodes"]["episodes"] == 21


async def test_movies_and_series_listed_together(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    await _add(c, "none")
    hd = await _hd(c)
    assert (await c.post("/titles", json={"tmdb_id": 438631, "profile_id": hd,
                                          "search": False})).status_code == 201  # fmt: skip
    items = (await c.get("/titles")).json()["items"]
    by_kind = {i["kind"]: i for i in items}
    assert by_kind["movie"]["episodes"] is None and len(by_kind["movie"]["wanted"]) == 1
    assert by_kind["series"]["episodes"]["monitored"] == 0
    movie = by_kind["movie"]
    assert (await c.get(f"/titles/{movie['id']}/episodes")).status_code == 409

    tmdb: FakeTmdb = fakes.tmdb
    assert tmdb.series[95396]["name"] == "Severance"
    sid = by_kind["series"]["id"]
    assert (await c.delete(f"/titles/{sid}")).status_code == 204  # episodes cascade
    assert len((await c.get("/wanted", params={"limit": 500})).json()["items"]) == 1
