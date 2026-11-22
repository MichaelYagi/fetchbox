"""Optional TVDB: absolute episode numbers (anime) filled in on a series refresh."""

from __future__ import annotations

import json
from typing import Any

import httpx

from fetchbox.providers.metadata.tvdb import TvdbClient, TvdbError
from tests.conftest import Running
from tests.pipeline import Fakes, standard_hd

KEY = "tvdb-key-123"


def tvdb_handler(calls: list[str], fallback: Any = None) -> Any:
    """TVDB v4: POST /login -> token; /series/{id}/episodes/default, paged by `links.next`."""
    eps = [{"seasonNumber": s, "number": n, "absoluteNumber": (s - 1) * 9 + n if s else None}
           for s, count in ((0, 1), (1, 9), (2, 10)) for n in range(1, count + 1)]  # fmt: skip

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.host != "api4.thetvdb.com":
            return fallback(req) if fallback else httpx.Response(404)
        calls.append(f"{req.method} {req.url.path}?{req.url.query.decode()}")
        if req.url.path == "/v4/login":
            ok = json.loads(req.content).get("apikey") == KEY
            return httpx.Response(200 if ok else 401, json={"data": {"token": "tok"}})
        if req.headers.get("authorization") != "Bearer tok":
            return httpx.Response(401)
        if req.url.path == "/v4/series/371980/episodes/default":
            page = int(req.url.params["page"])
            chunk = eps[page * 10 : page * 10 + 10]
            more = page * 10 + 10 < len(eps)
            return httpx.Response(200, json={"data": {"episodes": chunk},
                                             "links": {"next": "x" if more else None}})  # fmt: skip
        return httpx.Response(404)

    return handle


async def test_client_pages_and_errors() -> None:
    calls: list[str] = []
    c = TvdbClient(KEY, transport=httpx.MockTransport(tvdb_handler(calls)))
    nums = await c.absolute_numbers(371980)
    assert nums[(1, 1)] == 1 and nums[(2, 1)] == 10 and nums[(2, 10)] == 19
    assert (0, 1) not in nums  # specials have no absolute number
    assert [x.split("?")[0] for x in calls].count("POST /v4/login") == 1  # one token, 2 pages
    await c.aclose()
    bad = TvdbClient("wrong", transport=httpx.MockTransport(tvdb_handler([])))
    try:
        await bad.test()
        raise AssertionError("expected a TvdbError")
    except TvdbError as exc:
        assert "refused" in exc.message
    await bad.aclose()


async def test_refresh_fills_absolute_numbers(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    calls: list[str] = []
    disc = server.app.state.discovery
    disc.transport = httpx.MockTransport(tvdb_handler(calls, fakes.tmdb.handle))
    await disc.aclose()  # pick up the new transport
    r = await c.patch("/system/config", json={"tvdb": {"api_key": KEY}})
    assert r.status_code == 200, r.text
    body = {"kind": "series", "tmdb_id": 95396, "monitor_mode": "all",
            "profile_id": await standard_hd(c), "search": False}  # fmt: skip
    t = (await c.post("/titles", json=body)).json()
    seasons = (await c.get(f"/titles/{t['id']}/episodes")).json()
    absn = {
        (s["number"], e["number"]): e["absolute_number"] for s in seasons for e in s["episodes"]
    }
    assert absn[(1, 1)] == 1 and absn[(2, 3)] == 12
    assert any("episodes/default" in x for x in calls)
