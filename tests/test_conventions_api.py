"""ETag/If-Match, Idempotency-Key and sort/cursor conventions through the real API."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from tests.conftest import Running


async def _key(c: httpx.AsyncClient) -> dict[str, Any]:
    r = await c.post("/auth/keys", json={"name": "k", "scopes": ["media:read"]})
    assert r.status_code == 201
    body: dict[str, Any] = r.json()
    return body


# ---- ETag / If-Match ---------------------------------------------------------------------


async def test_etag_if_match_on_keys(client: httpx.AsyncClient) -> None:
    k = await _key(client)
    r = await client.get(f"/auth/keys/{k['id']}")
    tag = r.headers["etag"]
    assert tag == '"v1"'

    ok = await client.patch(f"/auth/keys/{k['id']}", json={"name": "a"}, headers={"If-Match": tag})
    assert ok.status_code == 200 and ok.headers["etag"] == '"v2"'

    stale = await client.patch(
        f"/auth/keys/{k['id']}", json={"name": "b"}, headers={"If-Match": tag}
    )
    assert stale.status_code == 412
    assert stale.json()["type"].endswith("/precondition-failed")
    assert (await client.get(f"/auth/keys/{k['id']}")).json()["name"] == "a"

    assert (
        await client.delete(f"/auth/keys/{k['id']}", headers={"If-Match": tag})
    ).status_code == 412
    assert (
        await client.post(f"/auth/keys/{k['id']}/revoke", headers={"If-Match": '"v2"'})
    ).status_code == 200
    assert (
        await client.delete(f"/auth/keys/{k['id']}", headers={"If-Match": "*"})
    ).status_code == 204


async def test_if_match_optional(client: httpx.AsyncClient) -> None:
    k = await _key(client)
    assert (await client.patch(f"/auth/keys/{k['id']}", json={"name": "x"})).status_code == 200


async def test_concurrent_write_is_412_not_lost_update(server: Running) -> None:
    """Two writers read v1; the second commit must fail even though both passed If-Match."""
    from sqlalchemy.orm.exc import StaleDataError

    from fetchbox.modules.auth.models import ApiKey

    async with server.client() as c:
        k = await _key(c)
    db = server.app.state.fb.db
    async with db.sessionmaker() as a, db.sessionmaker() as b:
        ka, kb = await a.get(ApiKey, k["id"]), await b.get(ApiKey, k["id"])
        assert ka is not None and kb is not None
        ka.name, kb.name = "from-a", "from-b"
        await a.commit()
        try:
            await b.commit()
            raise AssertionError("second writer should have failed")
        except StaleDataError:
            await b.rollback()


# ---- Idempotency-Key ---------------------------------------------------------------------


async def test_idempotent_replay(client: httpx.AsyncClient) -> None:
    h = {"Idempotency-Key": "abc-123"}
    first = await client.post("/system/selftest", json={"seconds": 0}, headers=h)
    again = await client.post("/system/selftest", json={"seconds": 0}, headers=h)
    assert first.status_code == again.status_code == 202
    assert first.json()["id"] == again.json()["id"]
    assert again.headers["idempotent-replayed"] == "true"
    assert again.headers["location"] == first.headers["location"]
    assert "idempotent-replayed" not in first.headers


async def test_idempotency_key_reused_with_other_body(client: httpx.AsyncClient) -> None:
    h = {"Idempotency-Key": "k1"}
    assert (
        await client.post("/system/selftest", json={"seconds": 0}, headers=h)
    ).status_code == 202
    r = await client.post("/system/selftest", json={"seconds": 1}, headers=h)
    assert r.status_code == 422 and r.json()["type"].endswith("/idempotency-mismatch")


async def test_without_key_each_post_creates(client: httpx.AsyncClient) -> None:
    a = await client.post("/system/selftest", json={"seconds": 0})
    b = await client.post("/system/selftest", json={"seconds": 0})
    assert a.json()["id"] != b.json()["id"]


async def test_keys_scoped_per_actor(server: Running, client: httpx.AsyncClient) -> None:
    other = (await client.post("/auth/keys", json={"name": "o", "scopes": ["system:write"]})).json()
    h = {"Idempotency-Key": "shared"}
    a = await client.post("/system/selftest", json={"seconds": 0}, headers=h)
    async with server.client(other["key"]) as oc:
        b = await oc.post("/system/selftest", json={"seconds": 0}, headers=h)
    assert a.json()["id"] != b.json()["id"]


async def test_errors_are_not_stored(client: httpx.AsyncClient) -> None:
    h = {"Idempotency-Key": "retry-me"}
    assert (await client.post("/automation/schedules/nope/run", headers=h)).status_code == 404
    # same key, same request: the failed attempt left nothing behind, so it runs again
    assert (await client.post("/automation/schedules/nope/run", headers=h)).status_code == 404
    rec = await client.post("/automation/schedules/nope/run", headers=h)
    assert "idempotent-replayed" not in rec.headers


async def test_in_progress_returns_409(server: Running) -> None:
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from fetchbox.core.idempotency import idempotent

    db = server.app.state.fb.db
    gate = asyncio.Event()

    def req() -> Request:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"{}", "more_body": False}

        return Request(
            {"type": "http", "method": "POST", "path": "/x", "query_string": b"", "headers": []},
            receive,
        )

    async def slow() -> JSONResponse:
        await gate.wait()
        return JSONResponse({"ok": 1}, status_code=201)

    first = asyncio.create_task(idempotent(db, req(), actor_id="a", key="k", run=slow))
    await asyncio.sleep(0.05)
    from fetchbox.core.errors import ProblemError

    try:
        await idempotent(db, req(), actor_id="a", key="k", run=slow)
        raise AssertionError("expected 409")
    except ProblemError as exc:
        assert exc.problem.status == 409 and exc.headers == {"Retry-After": "1"}
    gate.set()
    assert (await first).status_code == 201
    replay = await idempotent(db, req(), actor_id="a", key="k", run=slow)
    assert replay.status_code == 201 and replay.headers["idempotent-replayed"] == "true"


# ---- sort / cursor on a real list endpoint ---------------------------------------------------


async def test_jobs_sort_param(client: httpx.AsyncClient) -> None:
    ids = [
        (await client.post("/system/selftest", json={"seconds": 0})).json()["id"] for _ in range(3)
    ]
    only = {"filter[type]": "system.selftest"}
    asc = (await client.get("/jobs", params={**only, "sort": "created_at"})).json()["items"]
    assert [j["id"] for j in asc] == ids
    bad = await client.get("/jobs", params={"sort": "payload"})
    assert bad.status_code == 422 and bad.json()["errors"][0]["field"] == "sort"
