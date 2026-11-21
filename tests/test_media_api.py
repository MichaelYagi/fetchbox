"""Runtime config, discovery, titles and wanted items through the API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import update

from fetchbox.core.db import utcnow
from fetchbox.modules.media.models import Title
from tests.conftest import Running, running
from tests.fake_tmdb import V3_KEY, FakeTmdb


@pytest.fixture
async def tmdb(server: Running) -> FakeTmdb:
    fake = FakeTmdb()
    server.app.state.discovery.transport = fake.transport
    async with server.client() as c:
        r = await c.patch("/system/config", json={"tmdb": {"api_key": V3_KEY}})
        assert r.status_code == 200, r.text
    return fake


@pytest.fixture
async def c(server: Running, tmdb: FakeTmdb) -> AsyncIterator[httpx.AsyncClient]:
    async with server.client() as client:
        yield client


async def _hd(c: httpx.AsyncClient) -> str:
    profiles = (await c.get("/quality-profiles")).json()
    return str(next(p["id"] for p in profiles if p["name"] == "Standard HD"))


async def _add(c: httpx.AsyncClient, tmdb_id: int = 438631, **kw: Any) -> dict[str, Any]:
    r = await c.post("/titles", json={"tmdb_id": tmdb_id, "profile_id": await _hd(c), **kw})
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


# ---- config ---------------------------------------------------------------------------------


async def test_config_masks_secrets_and_versions(
    client: httpx.AsyncClient, server: Running
) -> None:
    r = await client.get("/system/config")
    assert r.status_code == 200 and r.headers["etag"] == '"v0"'
    assert r.json()["config"]["tmdb"] == {"api_key": None, "language": "en-US",
                                          "include_adult": False}  # fmt: skip

    r = await client.patch(
        "/system/config", json={"tmdb": {"api_key": V3_KEY}}, headers={"If-Match": '"v0"'}
    )
    assert r.status_code == 200 and r.headers["etag"] == '"v1"'
    assert r.json()["config"]["tmdb"]["api_key"] == "********"
    assert V3_KEY not in r.text

    # Stored encrypted, not in clear.
    from sqlalchemy import text

    async with server.app.state.fb.db.engine.connect() as conn:
        raw = (await conn.execute(text("SELECT data FROM app_config"))).scalar_one()
    assert V3_KEY not in raw and "enc:v1:" in raw

    # Sending the mask back keeps the key; other fields still change.
    r = await client.patch(
        "/system/config",
        json={"tmdb": {"api_key": "********", "language": "fr-FR"}, "search": {"min_seeders": 3}},
    )
    assert r.status_code == 200 and r.headers["etag"] == '"v2"'
    cfg = server.app.state.config.current
    assert cfg.tmdb.api_key.get_secret_value() == V3_KEY and cfg.tmdb.language == "fr-FR"
    assert cfg.search.min_seeders == 3

    stale = await client.patch("/system/config", json={"search": {"min_seeders": 1}},
                               headers={"If-Match": '"v1"'})  # fmt: skip
    assert stale.status_code == 412


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"tmdb": {"language": "french"}}, "body.tmdb.language"),
        ({"tmdb": {"apikey": "x"}}, "body.tmdb.apikey"),
        ({"nope": {}}, "body.nope"),
        ({"tmdb": "x"}, "body.tmdb"),
        ({"search": {"season_pack_share": 0}}, "body.search.season_pack_share"),
    ],
)
async def test_config_validation(
    client: httpx.AsyncClient, body: dict[str, Any], field: str
) -> None:
    r = await client.patch("/system/config", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["errors"][0]["field"] == field


async def test_config_change_is_logged_without_values(
    client: httpx.AsyncClient, server: Running
) -> None:
    sub = server.app.state.fb.bus.subscribe("system.config_changed")
    await client.patch("/system/config", json={"tmdb": {"api_key": V3_KEY}})
    ev = sub.queue.get_nowait()
    assert ev.data["fields"] == ["tmdb.api_key"] and V3_KEY not in ev.model_dump_json()
    await client.patch("/system/config", json={"tmdb": {"api_key": V3_KEY}})  # no change
    assert sub.queue.empty()


async def test_env_overrides_tmdb_key(settings: Any) -> None:
    env_settings = settings.model_copy(update={"tmdb_api_key": SecretStr("from-env")})
    async with running(env_settings) as srv, srv.client() as c:
        body = (await c.get("/system/config")).json()
        assert body["overridden"] == ["tmdb.api_key"]
        assert body["config"]["tmdb"]["api_key"] == "********"
        r = await c.patch("/system/config", json={"tmdb": {"api_key": "other"}})
        assert r.status_code == 409 and "environment" in r.json()["detail"]


# ---- discovery ------------------------------------------------------------------------------


async def test_discovery_needs_a_key(client: httpx.AsyncClient) -> None:
    r = await client.get("/discovery/search", params={"q": "dune"})
    assert r.status_code == 503
    assert r.json()["type"].endswith("metadata-not-configured")
    assert "themoviedb.org" in r.json()["detail"]


async def test_discovery(c: httpx.AsyncClient, tmdb: FakeTmdb) -> None:
    r = await c.get("/discovery/search", params={"q": "dune", "year": 2021})
    assert r.status_code == 200 and r.json()[0]["tmdb_id"] == 438631
    n = len(tmdb.requests)
    await c.get("/discovery/search", params={"q": "Dune ", "year": 2021})
    assert len(tmdb.requests) == n  # cached
    movie = (await c.get("/discovery/movies/438631")).json()
    assert movie["imdb_id"] == "tt1160419" and "Dune: Part One" in movie["alt_titles"]
    series = (await c.get("/discovery/series/95396")).json()
    assert [s["episode_count"] for s in series["seasons"]] == [9, 10]
    assert (await c.get("/discovery/search", params={"q": "sev", "kind": "series"})).json()
    r = await c.get("/discovery/movies/1")
    assert r.status_code == 404 and r.json()["type"].endswith("metadata-not-found")


async def test_discovery_bad_key_and_cache_reset(c: httpx.AsyncClient, tmdb: FakeTmdb) -> None:
    await c.patch("/system/config", json={"tmdb": {"api_key": "wrong"}})
    r = await c.get("/discovery/movies/438631")
    assert r.status_code == 502 and r.json()["type"].endswith("metadata-auth")
    health = (await c.get("/system/health")).json()
    metadata = next(x for x in health["checks"] if x["name"] == "metadata")
    assert metadata["status"] == "error" and "rejected" in metadata["message"]


# ---- titles ---------------------------------------------------------------------------------


async def test_add_movie_creates_wanted_item(c: httpx.AsyncClient) -> None:
    t = await _add(c)
    assert t["name"] == "Dune" and t["year"] == 2021 and t["kind"] == "movie"
    assert t["imdb_id"] == "tt1160419" and t["runtime"] == 155 and t["original_language"] == "en"
    assert t["monitor_mode"] == "all" and t["etag"] == '"v1"'
    [w] = t["wanted"]
    assert w["state"] == "missing" and w["monitored"] and not w["cutoff_met"]

    dup = await c.post("/titles", json={"tmdb_id": 438631, "profile_id": await _hd(c)})
    assert dup.status_code == 409 and t["id"] in dup.json()["detail"]

    wanted = (await c.get("/wanted", params={"state": "missing"})).json()["items"]
    assert [(x["id"], x["title_name"], x["title_year"]) for x in wanted] == [
        (w["id"], "Dune", 2021)
    ]
    assert (await c.get("/wanted", params={"state": "cutoff_unmet"})).json()["items"] == []


async def test_add_validation(c: httpx.AsyncClient) -> None:
    r = await c.post("/titles", json={"tmdb_id": 438631, "profile_id": "nope"})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "body.profile_id"
    r = await c.post("/titles", json={"tmdb_id": 1, "profile_id": await _hd(c)})
    assert r.status_code == 404
    bad = {"kind": "series", "tmdb_id": 95396, "profile_id": await _hd(c), "monitor_mode": "x"}
    r = await c.post("/titles", json=bad)
    assert r.status_code == 422 and r.json()["errors"][0]["field"].endswith("monitor_mode")
    assert (await c.get("/titles")).json()["items"] == []


async def test_list_sort_and_page(c: httpx.AsyncClient) -> None:
    await _add(c, 438631)
    await _add(c, 194, monitored=False)
    names = [t["name"] for t in (await c.get("/titles")).json()["items"]]
    assert names == ["Amélie", "Dune"]
    page = (await c.get("/titles", params={"sort": "-year", "limit": 1})).json()
    assert [t["name"] for t in page["items"]] == ["Dune"] and page["next_cursor"]
    rest = (await c.get("/titles", params={"sort": "-year", "cursor": page["next_cursor"]})).json()
    assert [t["name"] for t in rest["items"]] == ["Amélie"] and rest["next_cursor"] is None
    unmonitored = (await c.get("/wanted", params={"monitored": "false"})).json()["items"]
    assert [w["title_name"] for w in unmonitored] == ["Amélie"]


async def test_update_monitor_and_profile(c: httpx.AsyncClient) -> None:
    t = await _add(c)
    four_k = next(p["id"] for p in (await c.get("/quality-profiles")).json() if p["name"] == "4K")
    r = await c.patch(f"/titles/{t['id']}", json={"monitor_mode": "none", "profile_id": four_k},
                      headers={"If-Match": t["etag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile_id"] == four_k and body["monitor_mode"] == "none"
    assert body["wanted"][0]["monitored"] is False and body["etag"] == '"v2"'
    r = await c.patch(f"/titles/{t['id']}", json={"monitor_mode": "future"})
    assert r.status_code == 422
    r = await c.patch(f"/titles/{t['id']}", json={"name": "x"}, headers={"If-Match": '"v1"'})
    assert r.status_code == 412

    # Monitoring the wanted item flips the movie's monitor mode back.
    w = body["wanted"][0]
    r = await c.patch(
        f"/wanted/{w['id']}", json={"monitored": True}, headers={"If-Match": w["etag"]}
    )
    assert r.status_code == 200 and r.json()["monitored"] is True
    assert (await c.get(f"/titles/{t['id']}")).json()["monitor_mode"] == "all"


async def test_refresh_and_delete(c: httpx.AsyncClient, tmdb: FakeTmdb, server: Running) -> None:
    t = await _add(c)
    tmdb.movies[438631]["runtime"] = 156
    r = await c.post(f"/titles/{t['id']}/refresh")
    assert r.status_code == 200 and r.json()["runtime"] == 156  # bypasses the cache
    assert r.json()["refreshed_at"] >= t["refreshed_at"]

    hd = await _hd(c)
    r = await c.delete(f"/quality-profiles/{hd}")
    assert r.status_code == 409 and "used by" in r.json()["detail"]

    sub = server.app.state.fb.bus.subscribe("title.removed")
    assert (await c.delete(f"/titles/{t['id']}")).status_code == 204
    assert sub.queue.get_nowait().data["name"] == "Dune"
    assert (await c.get(f"/titles/{t['id']}")).status_code == 404
    assert (await c.get("/wanted")).json()["items"] == []  # cascaded
    assert (await c.delete(f"/quality-profiles/{hd}")).status_code == 204  # free now


async def test_activity_records_title_events(c: httpx.AsyncClient, server: Running) -> None:
    await _add(c)
    entries: list[dict[str, Any]] = []
    for _ in range(100):
        entries = (await c.get("/activity", params={"filter[category]": "title"})).json()["items"]
        if entries:
            break
        await asyncio.sleep(0.02)
    assert [e["message"] for e in entries] == ["Added Dune (2021)"]


async def test_scopes(server: Running, tmdb: FakeTmdb) -> None:
    async with server.client() as root:
        ro = (await root.post("/auth/keys", json={"name": "ro", "scopes": ["media:read"]})).json()
        disc = (
            await root.post("/auth/keys", json={"name": "d", "scopes": ["discovery:read"]})
        ).json()
        hd = await _hd(root)
    async with server.client(ro["key"]) as c:
        assert (await c.get("/titles")).status_code == 200
        assert (
            await c.post("/titles", json={"tmdb_id": 438631, "profile_id": hd})
        ).status_code == 403
        assert (await c.get("/discovery/search", params={"q": "dune"})).status_code == 403
        assert (await c.get("/system/config")).status_code == 403
    async with server.client(disc["key"]) as c:
        assert (await c.get("/discovery/search", params={"q": "dune"})).status_code == 200


async def test_scheduled_refresh_of_stale_titles(
    c: httpx.AsyncClient, tmdb: FakeTmdb, server: Running
) -> None:
    t = await _add(c)
    media = server.app.state.media
    assert await media.refresh_stale() == {"refreshed": 0, "failed": []}  # fresh

    async with server.app.state.fb.db.sessionmaker() as s:
        await s.execute(update(Title).values(refreshed_at=utcnow() - timedelta(days=8)))
        await s.commit()
    tmdb.movies[438631]["runtime"] = 157
    assert await media.refresh_stale() == {"refreshed": 1, "failed": []}
    after = (await c.get(f"/titles/{t['id']}")).json()
    assert after["runtime"] == 157 and after["refreshed_at"] > t["refreshed_at"]
    assert after["etag"] != t["etag"]

    rows = {r["name"]: r for r in (await c.get("/automation/schedules")).json()}
    assert rows["refresh_metadata"]["job_type"] == "media.refresh_metadata"
    assert rows["refresh_metadata"]["interval_seconds"] == 12 * 3600
