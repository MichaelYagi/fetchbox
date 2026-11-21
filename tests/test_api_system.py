from __future__ import annotations

import httpx

from fetchbox import migrations
from tests.conftest import Running


async def test_version(client: httpx.AsyncClient) -> None:
    r = await client.get("/system/version")
    assert r.status_code == 200
    assert r.json()["libtorrent"]


async def test_status_reports_schema_at_head(client: httpx.AsyncClient) -> None:
    body = (await client.get("/system/status")).json()
    assert body["database"] == "sqlite"
    assert body["schema_revision"] == migrations.head_revision()


async def test_health_ok_and_emits_change(client: httpx.AsyncClient, server: Running) -> None:
    bus = server.app.state.fb.bus
    sub = bus.subscribe("system.health_changed")
    body = (await client.get("/system/health")).json()
    # A fresh install has no TMDB key and no indexer: those warn (with fix links).
    checks = {c["name"]: c for c in body["checks"]}
    for name in ("metadata", "indexers"):
        assert checks[name]["status"] == "warning" and checks[name]["fix_url"]
    assert {n for n, c in checks.items() if c["status"] != "ok"} == {"metadata", "indexers"}
    assert {"database", "torrent_engine"} <= set(checks)
    assert body["status"] == "warning"
    events = []
    while not sub.queue.empty():
        events.append(sub.queue.get_nowait())
    assert events and events[-1].data["status"] == "warning"  # (startup's check may come first)
    await client.get("/system/health")
    assert sub.queue.empty()  # unchanged status -> no new event


async def test_problem_details_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "instance": "/api/v1/nope",
    }


async def test_openapi_served(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    assert spec["openapi"].startswith("3.1")
    assert "/api/v1/system/health" in spec["paths"]


async def test_sqlite_wal_enabled(server: Running) -> None:
    from sqlalchemy import text

    db = server.app.state.fb.db
    async with db.engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1
        assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() == 5000
