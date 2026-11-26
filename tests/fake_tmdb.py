"""A tiny in-process TMDB for tests (httpx.MockTransport)."""

from __future__ import annotations

from typing import Any

import httpx

V3_KEY = "0123456789abcdef0123456789abcdef"

DUNE: dict[str, Any] = {
    "id": 438631,
    "title": "Dune",
    "original_title": "Dune",
    "original_language": "en",
    "release_date": "2021-09-15",
    "runtime": 155,
    "status": "Released",
    "overview": "Paul Atreides...",
    "poster_path": "/d5NXSklXo0qyIYkgV94XAgMIckC.jpg",
    "imdb_id": "tt1160419",
    "genres": [{"id": 878, "name": "Science Fiction"}],
    "alternative_titles": {
        "titles": [
            {"iso_3166_1": "US", "title": "Dune: Part One", "type": ""},
            {"iso_3166_1": "FR", "title": "Dune : Première partie", "type": ""},
            {"iso_3166_1": "GB", "title": "Dune", "type": ""},
        ]
    },
    # (what append_to_response=images adds; a title may have none)
    "images": {
        "backdrops": [
            {"file_path": "/titled.jpg", "iso_639_1": "en", "vote_average": 9.0},
            {"file_path": "/sand.jpg", "iso_639_1": None, "vote_average": 5.5},
            {"file_path": "/worm.jpg", "iso_639_1": None, "vote_average": 7.1},
        ]
        + [{"file_path": f"/more{i}.jpg", "iso_639_1": None, "vote_average": 1} for i in range(9)]
    },
}

AMELIE = {
    "id": 194,
    "title": "Amélie",
    "original_title": "Le Fabuleux Destin d'Amélie Poulain",
    "original_language": "fr",
    "release_date": "2001-04-25",
    "runtime": 122,
    "status": "Released",
    "alternative_titles": {"titles": []},
}

SEVERANCE = {
    "id": 95396,
    "name": "Severance",
    "original_name": "Severance",
    "original_language": "en",
    "first_air_date": "2022-02-17",
    "status": "Returning Series",
    "episode_run_time": [],
    "seasons": [{"season_number": 1}, {"season_number": 2}],
    "external_ids": {"imdb_id": "tt11280740", "tvdb_id": 371980},
    "alternative_titles": {"results": [{"iso_3166_1": "DE", "title": "Severance"}]},
}


class FakeTmdb:
    def __init__(self, key: str = V3_KEY) -> None:
        self.key = key
        self.requests: list[httpx.Request] = []
        self.movies = {DUNE["id"]: dict(DUNE), AMELIE["id"]: dict(AMELIE)}
        self.series = {SEVERANCE["id"]: dict(SEVERANCE)}
        # (series id, season) -> episodes; tests add specials, future episodes, seasons
        self.seasons: dict[tuple[int, int], list[dict[str, Any]]] = {
            (95396, n): [
                {
                    "episode_number": e,
                    "name": f"Ep {e}",
                    "air_date": f"2022-0{n}-1{e % 10}",
                    **({"still_path": f"/s{n}e{e}.jpg"} if e == 1 else {}),
                }
                for e in range(1, (9 if n == 1 else 10) + 1)
            ]
            for n in (1, 2)
        }
        self.fail_next: list[int] = []  # status codes to return before succeeding

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _authorized(self, req: httpx.Request) -> bool:
        return (
            req.url.params.get("api_key") == self.key
            or req.headers.get("authorization") == f"Bearer {self.key}"
        )

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.fail_next:
            code = self.fail_next.pop(0)
            return httpx.Response(code, headers={"Retry-After": "0"}, json={})
        if not self._authorized(req):
            return httpx.Response(401, json={"status_code": 7, "status_message": "Invalid key"})
        path = req.url.path.removeprefix("/3")
        q = req.url.params
        if path == "/configuration":
            return httpx.Response(200, json={"images": {}})
        if path == "/search/movie":
            term = q["query"].lower()
            hits = [m for m in self.movies.values() if term in m["title"].lower()]
            if "year" in q:
                hits = [m for m in hits if m["release_date"].startswith(q["year"])]
            return httpx.Response(200, json={"results": hits, "total_pages": 1})
        if path == "/search/tv":
            hits = [SEVERANCE] if q["query"].lower() in "severance" else []
            return httpx.Response(200, json={"results": hits})
        if path.startswith("/movie/"):
            m = self.movies.get(int(path.split("/")[2]))
            return httpx.Response(200, json=m) if m else httpx.Response(404, json={})
        if path.startswith("/tv/"):
            parts = path.split("/")  # ["", "tv", id] or ["", "tv", id, "season", n]
            tv = self.series.get(int(parts[2]))
            if tv is None:
                return httpx.Response(404, json={})
            if len(parts) == 3:
                numbers = sorted(n for (sid, n) in self.seasons if sid == tv["id"])
                return httpx.Response(
                    200, json={**tv, "seasons": [{"season_number": n} for n in numbers]}
                )
            eps = self.seasons.get((tv["id"], int(parts[4])))
            if eps is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"name": f"Season {parts[4]}", "episodes": eps})
        if path.startswith("/find/"):
            if path == "/find/tt1160419":
                return httpx.Response(200, json={"movie_results": [DUNE], "tv_results": []})
            return httpx.Response(200, json={"movie_results": [], "tv_results": []})
        return httpx.Response(404, json={})
