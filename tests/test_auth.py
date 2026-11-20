from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest

from fetchbox.config import Settings
from fetchbox.core.db import utcnow
from fetchbox.modules.auth.service import parse_key
from tests.conftest import Running, running


async def _key(c: httpx.AsyncClient, scopes: list[str], **extra: object) -> dict[str, object]:
    r = await c.post("/auth/keys", json={"name": "k", "scopes": scopes, **extra})
    assert r.status_code == 201, r.text
    body: dict[str, object] = r.json()
    return body


# ---- API keys ------------------------------------------------------------------------


async def test_bootstrap_default_key(server: Running, client: httpx.AsyncClient) -> None:
    me = (await client.get("/auth/me")).json()
    assert me == {
        "type": "api_key",
        "id": me["id"],
        "name": "default",
        "scopes": ["*"],
        "via": "api_key",
    }
    assert parse_key(server.root_key) is not None


async def test_bootstrap_runs_once(settings: Settings) -> None:
    async with running(settings):
        pass
    with pytest.raises(AssertionError, match="bootstrap key was not issued"):
        async with running(settings):
            pass


async def test_no_credentials_401_problem(server: Running) -> None:
    async with server.client("") as anon:
        r = await anon.get("/system/status")
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
        assert r.headers["content-type"].startswith("application/problem+json")
        assert (await anon.get("/system/ping")).json() == {"status": "ok"}


@pytest.mark.parametrize("bad", ["nope", "fb_aaaaaaaa_wrong", "fb_short_x"])
async def test_bad_key_401(server: Running, bad: str) -> None:
    async with server.client(bad) as c:
        assert (await c.get("/system/status")).status_code == 401


async def test_x_api_key_header(server: Running) -> None:
    async with server.client("") as c:
        r = await c.get("/auth/me", headers={"X-Api-Key": server.root_key})
        assert r.status_code == 200


async def test_scoped_key_enforced(server: Running, client: httpx.AsyncClient) -> None:
    ro = await _key(client, ["system:read"])
    async with server.client(str(ro["key"])) as c:
        assert (await c.get("/system/status")).status_code == 200
        r = await c.post("/jobs/01JD000000000000000000000X/cancel")
        assert r.status_code == 403 and r.json()["type"].endswith("/missing-scope")
        assert (await c.get("/auth/keys")).status_code == 403
    rw = await _key(client, ["system:write"])
    async with server.client(str(rw["key"])) as c:
        assert (await c.get("/jobs")).status_code == 200  # write implies read


async def test_key_shown_once_and_hash_only(server: Running, client: httpx.AsyncClient) -> None:
    created = await _key(client, ["media:read"])
    listed = (await client.get("/auth/keys")).json()
    row = next(k for k in listed if k["id"] == created["id"])
    assert "key" not in row and "key_hash" not in row
    assert str(created["key"]).startswith(f"fb_{row['prefix']}_")


async def test_cannot_escalate(server: Running, client: httpx.AsyncClient) -> None:
    admin = await _key(client, ["auth:admin", "media:read"])
    async with server.client(str(admin["key"])) as c:
        ok = await c.post("/auth/keys", json={"name": "x", "scopes": ["media:read"]})
        assert ok.status_code == 201
        for scopes in (["media:write"], ["*"], ["system:read"]):
            r = await c.post("/auth/keys", json={"name": "x", "scopes": scopes})
            assert r.status_code == 403, scopes
            assert r.json()["type"].endswith("/scope-escalation")
        r = await c.patch(f"/auth/keys/{admin['id']}", json={"scopes": ["*"]})
        assert r.status_code == 403


async def test_unknown_scope_422(client: httpx.AsyncClient) -> None:
    r = await client.post("/auth/keys", json={"name": "x", "scopes": ["media:delete"]})
    assert r.status_code == 422
    assert r.json()["errors"][0]["field"] == "scopes"


async def test_revoke_rotate_delete(server: Running, client: httpx.AsyncClient) -> None:
    k = await _key(client, ["system:read"])
    async with server.client(str(k["key"])) as c:
        assert (await c.get("/system/status")).status_code == 200  # now cached

    rotated = (await client.post(f"/auth/keys/{k['id']}/rotate")).json()
    assert rotated["id"] == k["id"] and rotated["key"] != k["key"]
    async with server.client(str(k["key"])) as old:
        assert (await old.get("/system/status")).status_code == 401  # cache invalidated
    async with server.client(rotated["key"]) as new:
        assert (await new.get("/system/status")).status_code == 200

    assert (await client.post(f"/auth/keys/{k['id']}/revoke")).json()["revoked_at"]
    async with server.client(rotated["key"]) as new:
        assert (await new.get("/system/status")).status_code == 401
    assert (await client.post(f"/auth/keys/{k['id']}/rotate")).status_code == 409

    assert (await client.delete(f"/auth/keys/{k['id']}")).status_code == 204
    assert (await client.delete(f"/auth/keys/{k['id']}")).status_code == 404


async def test_expiry(server: Running, client: httpx.AsyncClient) -> None:
    past = (utcnow() - timedelta(minutes=1)).isoformat()
    r = await client.post(
        "/auth/keys", json={"name": "x", "scopes": ["system:read"], "expires_at": past}
    )
    assert r.status_code == 422

    k = await _key(client, ["system:read"], expires_at=(utcnow() + timedelta(hours=1)).isoformat())
    async with server.client(str(k["key"])) as c:
        assert (await c.get("/system/status")).status_code == 200
    # Expire it in place; cached principal must stop working too.
    await client.patch(
        f"/auth/keys/{k['id']}",
        json={"expires_at": (utcnow() + timedelta(milliseconds=1)).isoformat()},
    )
    async with server.client(str(k["key"])) as c:
        assert (await c.get("/system/status")).status_code == 401


async def test_last_used_recorded(server: Running, client: httpx.AsyncClient) -> None:
    k = await _key(client, ["system:read"])
    async with server.client(str(k["key"])) as c:
        await c.get("/system/status")
    await server.app.state.auth.drain()
    row = next(x for x in (await client.get("/auth/keys")).json() if x["id"] == k["id"])
    assert row["last_used_at"] is not None and row["last_used_ip"] == "127.0.0.1"


async def test_key_events_emitted(server: Running, client: httpx.AsyncClient) -> None:
    sub = server.app.state.fb.bus.subscribe("auth.*")
    k = await _key(client, ["media:read"])
    await client.post(f"/auth/keys/{k['id']}/revoke")
    events = [sub.queue.get_nowait() for _ in range(2)]
    assert [e.type for e in events] == ["auth.key_created", "auth.key_revoked"]
    assert events[0].actor.name == "default"
    assert "key" not in events[0].data  # never leak the secret into events


# ---- UI sessions -----------------------------------------------------------------------


@asynccontextmanager
async def ui_session(
    server: Running, password: str = "correct-horse"
) -> AsyncIterator[httpx.AsyncClient]:
    """First-run setup, yielding a cookie-authenticated client with its CSRF header set."""
    async with server.client("") as c:
        r = await c.post("/auth/setup", json={"password": password})
        assert r.status_code == 200, r.text
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        yield c


async def _setup_only(server: Running) -> None:
    async with ui_session(server):
        pass


async def test_setup_then_session_works(server: Running) -> None:
    async with ui_session(server) as ui:
        me = (await ui.get("/auth/me")).json()
        assert me["type"] == "user" and me["via"] == "session" and me["name"] == "admin"
        assert (await ui.post("/auth/setup", json={"password": "another-pass"})).status_code == 409


async def test_setup_rejects_short_password(server: Running) -> None:
    async with server.client("") as c:
        r = await c.post("/auth/setup", json={"password": "short"})
        assert r.status_code == 422


async def test_setup_local_only(server: Running) -> None:
    transport = httpx.ASGITransport(app=server.app, client=("192.168.1.50", 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        r = await c.post("/auth/setup", json={"password": "correct-horse"})
        assert r.status_code == 403


async def test_session_cookie_flags(server: Running) -> None:
    async with server.client("") as c:
        r = await c.post("/auth/setup", json={"password": "correct-horse"})
    cookies = r.headers.get_list("set-cookie")
    session = next(x for x in cookies if x.startswith("fb_session="))
    csrf = next(x for x in cookies if x.startswith("fb_csrf="))
    assert "HttpOnly" in session and "SameSite=strict" in session
    assert "HttpOnly" not in csrf


async def test_csrf_required_for_cookie_writes(server: Running, client: httpx.AsyncClient) -> None:
    async with ui_session(server) as ui:
        token = ui.headers.pop("X-CSRF-Token")
        assert (await ui.get("/auth/keys")).status_code == 200  # reads need no CSRF
        r = await ui.post("/auth/keys", json={"name": "x", "scopes": ["media:read"]})
        assert r.status_code == 403 and r.json()["type"].endswith("/csrf")
        r = await ui.post(
            "/auth/keys",
            json={"name": "x", "scopes": ["media:read"]},
            headers={"X-CSRF-Token": "wrong"},
        )
        assert r.status_code == 403
        r = await ui.post(
            "/auth/keys",
            json={"name": "x", "scopes": ["media:read"]},
            headers={"X-CSRF-Token": token},
        )
        assert r.status_code == 201


async def test_login_logout(server: Running) -> None:
    await _setup_only(server)
    async with server.client("") as c:
        bad = await c.post("/auth/login", json={"username": "admin", "password": "nope-nope"})
        assert bad.status_code == 401
        r = await c.post("/auth/login", json={"username": "admin", "password": "correct-horse"})
        assert r.status_code == 200
        csrf = r.json()["csrf_token"]
        assert (await c.get("/auth/me")).status_code == 200
        assert (await c.post("/auth/logout", headers={"X-CSRF-Token": csrf})).status_code == 204
        assert (await c.get("/auth/me")).status_code == 401


async def test_login_rate_limited(server: Running) -> None:
    await _setup_only(server)
    async with server.client("") as c:
        for _ in range(10):
            r = await c.post("/auth/login", json={"username": "admin", "password": "wrong-pass"})
            assert r.status_code == 401
        r = await c.post("/auth/login", json={"username": "admin", "password": "correct-horse"})
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


async def test_password_change_signs_out_sessions(server: Running) -> None:
    async with ui_session(server) as ui:
        assert (await ui.get("/auth/me")).status_code == 200
        await server.app.state.auth.set_password("brand-new-pass")
        assert (await ui.get("/auth/me")).status_code == 401
