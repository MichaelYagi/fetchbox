"""Indexers and interactive search through the API, with fake TMDB and Torznab."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx

from fetchbox.core.db import utcnow
from fetchbox.modules.sources.service import backoff
from tests.conftest import Running
from tests.fake_torznab import API_KEY
from tests.pipeline import ITEMS, Fakes, _indexer, _movie

# ---- indexers -------------------------------------------------------------------------------


async def test_indexer_crud_masks_the_key(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    kinds = (await c.get("/indexers/kinds")).json()
    assert [k["kind"] for k in kinds] == ["definition", "prowlarr", "torznab"]
    assert "api_key" in kinds[0]["settings_schema"]["properties"]

    ix = await _indexer(c, fakes, "one", [])
    assert ix["settings"]["api_key"] == "********" and ix["priority"] == 25
    assert ix["settings"]["url"] == "http://one" and ix["etag"] == '"v1"'

    from sqlalchemy import text

    async with server.app.state.fb.db.engine.connect() as conn:
        raw = (await conn.execute(text("SELECT settings FROM indexer"))).scalar_one()
    assert API_KEY not in raw

    # MASK keeps the key; changing the URL works
    r = await c.patch(
        f"/indexers/{ix['id']}",
        headers={"If-Match": '"v1"'},
        json={"settings": {"api_key": "********", "url": "http://one/"}, "priority": 5},
    )
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == 5 and r.json()["etag"] == '"v2"'
    assert (await c.post(f"/indexers/{ix['id']}/test")).json()["ok"] is True

    assert (
        await c.post("/indexers", json={"name": "one", "settings": {"url": "http://x"}})
    ).status_code == 409
    r = await c.post("/indexers", json={"name": "bad", "settings": {"url": "ftp://x"}})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "body.settings.url"
    r = await c.post("/indexers", json={"name": "bad", "kind": "nope", "settings": {}})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "body.kind"

    assert (await c.delete(f"/indexers/{ix['id']}")).status_code == 204
    assert (await c.get("/indexers")).json() == []


async def test_reorder(c: httpx.AsyncClient, fakes: Fakes) -> None:
    a = await _indexer(c, fakes, "a", [])
    b = await _indexer(c, fakes, "b", [])
    r = await c.post("/indexers/reorder", json={"ids": [b["id"], a["id"]]})
    assert [(x["name"], x["priority"]) for x in r.json()] == [("b", 1), ("a", 2)]
    assert (await c.post("/indexers/reorder", json={"ids": ["nope"]})).status_code == 422


def test_backoff_schedule() -> None:
    assert backoff(1) == timedelta(minutes=5)
    assert backoff(3) == timedelta(minutes=20)
    assert backoff(40) == timedelta(hours=24)
    assert backoff(1, retry_after=90) == timedelta(seconds=90)


async def test_health(c: httpx.AsyncClient, fakes: Fakes) -> None:
    def check(body: dict[str, Any]) -> dict[str, Any]:
        return next(x for x in body["checks"] if x["name"] == "indexers")

    assert check((await c.get("/system/health")).json())["status"] == "warning"
    await _indexer(c, fakes, "one", [])
    assert check((await c.get("/system/health")).json())["status"] == "ok"


# ---- interactive search ---------------------------------------------------------------------


async def test_interactive_search(c: httpx.AsyncClient, fakes: Fakes, server: Running) -> None:
    movie = await _movie(c)
    wid = movie["wanted"][0]["id"]
    await _indexer(c, fakes, "one", ITEMS[:3])
    await _indexer(c, fakes, "two", [ITEMS[0], *ITEMS[3:]], priority=10)  # duplicate of FLUX
    sub = server.app.state.fb.bus.subscribe("search.completed")

    r = await c.get("/releases", params={"wanted_item_id": wid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["indexers_searched"] == 2 and body["failures"] == []
    rel = body["releases"]
    titles = [x["candidate"]["title"] for x in rel]
    assert titles[:2] == ["Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX",
                          "Dune.2021.720p.BluRay.x264-SPARKS"]  # fmt: skip
    assert len(titles) == 5  # FLUX listed once
    flux = rel[0]
    assert flux["accepted"] and flux["candidate"]["source_name"] == "two"  # better priority
    assert flux["format_score"] == 5 and flux["tier"] == "WEB 1080p"
    reasons = {x["candidate"]["title"]: [j["code"] for j in x["rejections"]] for x in rel}
    assert reasons["Dune.2021.2160p.WEB-DL.HEVC-GRP"] == ["quality_not_allowed"]
    assert "wrong_year" in reasons["Dune.Part.Two.2024.1080p.WEB-DL-GRP"]
    assert reasons["Dune.2021.1080p.WEB-DL.HC-NOPE"] == ["too_few_seeders", "format_score_too_low"]
    assert body["picks"] == [flux["release_id"]]
    assert (
        server.app.state.releases.cached(flux["release_id"]).decision.candidate.info_hash
        == "a" * 40
    )

    # searched via IMDb id, movie categories
    assert fakes.torznab["one"].last("movie")["imdbid"] == "1160419"
    ev = sub.queue.get_nowait()
    assert ev.data["results"] == 5 and ev.data["accepted"] == 2
    wanted = (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]
    assert wanted["last_search_at"] and wanted["etag"] == movie["wanted"][0]["etag"]


async def test_failing_indexer_is_reported_and_backs_off(
    c: httpx.AsyncClient, fakes: Fakes
) -> None:
    movie = await _movie(c)
    wid = movie["wanted"][0]["id"]
    good = await _indexer(c, fakes, "good", ITEMS[:1])
    bad = await _indexer(c, fakes, "bad", [])
    fakes.torznab["bad"].fail = 503

    body = (await c.get("/releases", params={"wanted_item_id": wid})).json()
    assert [f["indexer_name"] for f in body["failures"]] == ["bad"]
    assert len(body["releases"]) == 1
    row = (await c.get(f"/indexers/{bad['id']}")).json()
    assert row["failure_count"] == 1 and row["last_error"] and row["disabled_until"]
    assert row["etag"] == bad["etag"]  # runtime state doesn't change the ETag

    # next search skips it
    n = len(fakes.torznab["bad"].requests)
    body = (await c.get("/releases", params={"wanted_item_id": wid})).json()
    assert body["indexers_searched"] == 1 and len(fakes.torznab["bad"].requests) == n

    health = (await c.get("/system/health")).json()
    ix = next(x for x in health["checks"] if x["name"] == "indexers")
    assert ix["status"] == "warning" and "bad" in ix["message"]

    # a passing test clears the backoff
    fakes.torznab["bad"].fail = None
    assert (await c.post(f"/indexers/{bad['id']}/test")).json()["ok"]
    row = (await c.get(f"/indexers/{bad['id']}")).json()
    assert row["failure_count"] == 0 and row["disabled_until"] is None
    assert good["id"]


async def test_interactive_search_errors(c: httpx.AsyncClient, fakes: Fakes) -> None:
    assert (await c.get("/releases", params={"wanted_item_id": "nope"})).status_code == 404
    movie = await _movie(c)
    body = (await c.get("/releases", params={"wanted_item_id": movie["wanted"][0]["id"]})).json()
    assert body["indexers_searched"] == 0 and body["releases"] == []  # no indexers yet


async def test_disabled_indexer_is_skipped(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    ix = await _indexer(c, fakes, "off", ITEMS[:1], enabled=False)
    assert ix["enabled"] is False
    assert await server.app.state.sources.release_sources() == []
    sources = await server.app.state.sources.release_sources(now=utcnow() + timedelta(days=2))
    assert sources == []


async def test_scopes(server: Running, fakes: Fakes) -> None:
    async with server.client() as root:
        ro = (await root.post("/auth/keys", json={"name": "ro", "scopes": ["sources:read"]})).json()
    async with server.client(ro["key"]) as c:
        assert (await c.get("/indexers")).status_code == 200
        r = await c.post("/indexers", json={"name": "x", "settings": {"url": "http://x"}})
        assert r.status_code == 403
        assert (await c.get("/releases", params={"wanted_item_id": "x"})).status_code == 403


async def test_failures_at_once_all_count(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    """Searches failing together on one indexer each count (no lost increments)."""
    import asyncio

    from fetchbox.providers.indexers import IndexerError

    ix = await _indexer(c, fakes, "one", ITEMS)
    svc = server.app.state.sources
    await asyncio.gather(
        *(svc.record_failure(ix["id"], IndexerError("unavailable", "down")) for _ in range(6))
    )
    row = (await c.get(f"/indexers/{ix['id']}")).json()
    assert row["failure_count"] == 6 and row["etag"] == ix["etag"]  # user ETag unchanged
