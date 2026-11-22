"""Behind a reverse proxy: real client addresses, and sign-in by the proxy's header,
both only from proxies listed in Settings > Network."""

from __future__ import annotations

import httpx
from starlette.requests import Request

from fetchbox.modules.auth.deps import client_ip, request_host
from tests.conftest import Running

PROXY = ("10.0.0.5", 4000)
ELSEWHERE = ("192.168.1.9", 4000)


def _req(server: Running, peer: tuple[str, int], headers: dict[str, str]) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": peer, "app": server.app,
    })  # fmt: skip


def _anon(server: Running, peer: tuple[str, int], **headers: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app, client=peer),
                             base_url="http://fetchbox:8787/api/v1", headers=headers)  # fmt: skip


async def test_trusted_proxy_and_header_sign_in(c: httpx.AsyncClient, server: Running) -> None:
    r = await c.patch("/system/config", json={"network": {"trusted_proxies": ["nonsense"]}})
    assert r.status_code == 422 and "isn't an IP address" in r.text
    fwd = {"X-Forwarded-For": "6.6.6.6, 203.0.113.7", "X-Forwarded-Host": "media.example"}
    # nothing trusted yet: the headers are ignored
    assert client_ip(_req(server, PROXY, fwd)) == "10.0.0.5"

    r = await c.patch("/system/config", json={"network": {
        "trusted_proxies": ["10.0.0.0/24", " ", "::1"], "auth_header": "Remote-User"}})  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["config"]["network"]["trusted_proxies"] == ["10.0.0.0/24", "::1/128"]

    # the last hop the trusted proxy saw; 6.6.6.6 was written by the client itself
    assert client_ip(_req(server, PROXY, fwd)) == "203.0.113.7"
    assert request_host(_req(server, PROXY, fwd)) == "media.example"
    # from anyone else: their own address, whatever they claim
    assert client_ip(_req(server, ELSEWHERE, fwd)) == "192.168.1.9"
    assert (
        request_host(_req(server, ELSEWHERE, {**fwd, "Host": "fetchbox:8787"})) == "fetchbox:8787"
    )
    assert client_ip(_req(server, PROXY, {"X-Forwarded-For": "not-an-ip"})) == "10.0.0.5"

    async with _anon(server, PROXY, **{"Remote-User": "alice"}) as p:
        me = (await p.get("/auth/me")).json()
        assert me["name"] == "alice" and me["via"] == "proxy"
        # a write from the proxied site itself works...
        site = {"Origin": "https://media.example", "X-Forwarded-Host": "media.example"}
        assert (await p.post("/downloads/pause-all", headers=site)).status_code == 200
        # ...one another site makes the signed-in browser send doesn't
        evil = {"Origin": "https://evil.example", "X-Forwarded-Host": "media.example"}
        assert (await p.post("/downloads/pause-all", headers=evil)).status_code == 403
        r = await p.post("/downloads/pause-all", headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
    # the same header from somewhere other than the proxy signs nobody in
    async with _anon(server, ELSEWHERE, **{"Remote-User": "alice"}) as p:
        assert (await p.get("/auth/me")).status_code == 401
    # an empty header isn't a person
    async with _anon(server, PROXY, **{"Remote-User": " "}) as p:
        assert (await p.get("/auth/me")).status_code == 401

    # header sign-in off again: the proxy's header means nothing
    await c.patch("/system/config", json={"network": {"auth_header": None}})
    async with _anon(server, PROXY, **{"Remote-User": "alice"}) as p:
        assert (await p.get("/auth/me")).status_code == 401
