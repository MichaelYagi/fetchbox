from __future__ import annotations

import httpx

from fetchbox import migrations


async def test_version(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/system/version")
    assert r.status_code == 200
    assert r.json()["libtorrent"]


async def test_status_reports_schema_at_head(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/v1/system/status")).json()
    assert body["database"] == "sqlite"
    assert body["schema_revision"] == migrations.head_revision()


async def test_health_ok_and_emits_change(client: httpx.AsyncClient) -> None:
    bus = client.app.state.fb.bus  # type: ignore[attr-defined]
    sub = bus.subscribe("system.health_changed")
    body = (await client.get("/api/v1/system/health")).json()
    assert body["status"] == "ok", body
    assert {c["name"] for c in body["checks"]} >= {"database", "torrent_engine"}
    assert sub.queue.get_nowait().data["status"] == "ok"
    await client.get("/api/v1/system/health")
    assert sub.queue.empty()  # unchanged status -> no new event


async def test_problem_details_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "instance": "/api/v1/nope",
    }


async def test_openapi_served(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/api/v1/openapi.json")).json()
    assert spec["openapi"].startswith("3.1")
    assert "/api/v1/system/health" in spec["paths"]


async def test_sqlite_wal_enabled(client: httpx.AsyncClient) -> None:
    from sqlalchemy import text

    db = client.app.state.fb.db  # type: ignore[attr-defined]
    async with db.engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1
