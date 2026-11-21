"""Calendar: episodes airing and movie release dates in a date range."""

from __future__ import annotations

from datetime import date

import httpx

from tests.pipeline import Fakes, _movie, standard_hd


async def test_calendar_lists_episodes_and_movie_dates(c: httpx.AsyncClient, fakes: Fakes) -> None:
    fakes.tmdb.movies[438631]["release_dates"] = {
        "results": [
            {"iso_3166_1": "US", "release_dates": [
                {"type": 3, "release_date": "2022-01-14T00:00:00.000Z"},
                {"type": 4, "release_date": "2022-02-01T00:00:00.000Z"}]},
        ]
    }  # fmt: skip
    movie = await _movie(c, search=False)
    body = {"kind": "series", "tmdb_id": 95396, "monitor_mode": "all",
            "profile_id": await standard_hd(c), "search": False}  # fmt: skip
    show = (await c.post("/titles", json=body)).json()

    r = await c.get("/calendar", params={"start": "2022-01-10", "end": "2022-02-12"})
    assert r.status_code == 200, r.text
    rows = r.json()
    days = [x["date"] for x in rows]
    assert days == sorted(days)
    eps = [(x["season"], x["episode"]) for x in rows if x["kind"] == "episode"]
    assert eps == [(1, n) for n in range(1, 10)] + [(2, 10), (2, 1), (2, 2)]
    assert all(x["title_id"] == show["id"] for x in rows if x["kind"] == "episode")
    films = [(x["date"], x["release"]) for x in rows if x["kind"] == "movie"]
    assert films == [("2022-01-14", "cinema"), ("2022-02-01", "digital")]
    assert all(x["title_id"] == movie["id"] and x["state"] == "missing"
               for x in rows if x["kind"] == "movie")  # fmt: skip
    first = rows[0]
    assert first["episode_name"] == "Ep 1" and first["monitored"]

    assert (
        await c.get("/calendar", params={"start": "2022-02-12", "end": "2022-01-10"})
    ).status_code == 422
    far = {"start": "2022-01-01", "end": date(2022, 6, 1).isoformat()}
    assert (await c.get("/calendar", params=far)).status_code == 422
