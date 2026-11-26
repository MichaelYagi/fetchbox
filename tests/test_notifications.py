"""Notifications: events reach the targets that want them, as short messages."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest

from fetchbox.core.db import utcnow
from fetchbox.core.secrets import MASK
from fetchbox.providers.notifiers import Notification
from fetchbox.providers.notifiers.discord import embed
from tests.conftest import Running
from tests.torrents import until

DISCORD = "https://discord.com/api/webhooks/123456789012345678/abcDEF-ghi_jkl"


class Receiver:
    """A real HTTP server on localhost that records what's POSTed to it."""

    def __init__(self) -> None:
        self.got: list[tuple[dict[str, str], dict[str, Any]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.got.append((dict(self.headers), json.loads(body)))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a: Any) -> None:
                pass

        self.http = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_port}/hook"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    r = Receiver()
    yield r
    r.http.shutdown()


async def test_webhook_receives_the_events_it_wants(
    c: httpx.AsyncClient, server: Running, receiver: Receiver
) -> None:
    kinds = (await c.get("/notifications/events")).json()
    defaults = [k["event"] for k in kinds if k["default"]]
    assert "import.completed" in defaults and "release.grabbed" not in defaults
    r = await c.post("/notifications/targets", json={"kind": "webhook", "name": "Home",
                     "settings": {"url": receiver.url, "secret": "s3cret"}})  # fmt: skip
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["events"] == defaults and t["settings"]["secret"] == MASK

    bus = server.app.state.fb.bus
    await bus.emit("import.completed", name="Dune", year=2021,
                   path="Dune (2021)/Dune (2021).mkv", quality="WEBDL-1080p")  # fmt: skip
    await bus.emit("release.grabbed", name="Dune", release_title="x")  # not selected
    await bus.emit("download.failed", name="Dune.2021.1080p", error="the tracker is down")

    async def two() -> bool:
        return len(receiver.got) >= 2

    await until(two, within=10)
    (h1, b1), (_h2, b2) = sorted(receiver.got, key=lambda x: x[1]["event"], reverse=True)
    assert b1["event"] == "import.completed" and b1["title"] == "Downloaded Dune (2021)"
    assert b1["body"] == "Dune (2021)/Dune (2021).mkv\nWEBDL-1080p" and b1["level"] == "success"
    assert h1["X-Fetchbox-Secret"] == "s3cret" and h1["X-Fetchbox-Event"] == "import.completed"
    assert b2["title"] == "Download failed: Dune.2021.1080p" and b2["level"] == "error"
    assert len(receiver.got) == 2

    # choose events; turn off; test
    change = {"events": ["release.grabbed"], "settings": {"url": receiver.url, "secret": MASK}}
    r = await c.patch(f"/notifications/targets/{t['id']}", json=change)
    assert r.status_code == 200 and r.json()["events"] == ["release.grabbed"]
    receiver.got.clear()
    res = (await c.post(f"/notifications/targets/{t['id']}/test")).json()
    assert res["ok"], res
    assert receiver.got[0][1]["title"] == "Fetchbox test"
    assert receiver.got[0][0]["X-Fetchbox-Secret"] == "s3cret"  # the masked secret was kept
    bad = await c.patch(f"/notifications/targets/{t['id']}", json={"events": ["nope"]})
    assert bad.status_code == 422


async def test_a_send_never_makes_saving_the_target_fail(
    c: httpx.AsyncClient, server: Running, receiver: Receiver
) -> None:
    """Recording that a message went out is bookkeeping: a save made with the ETag read
    before the send still goes through (it was a 412 when the save and send overlapped)."""
    r = await c.post("/notifications/targets", json={"kind": "webhook", "name": "Hook",
                     "settings": {"url": receiver.url}})  # fmt: skip
    assert r.status_code == 201, r.text
    t = r.json()
    tag = t["etag"]

    await server.app.state.fb.bus.emit("import.completed", name="Dune", year=2021, path="x")

    async def mine() -> dict[str, Any]:
        return next(x for x in (await c.get("/notifications/targets")).json() if x["id"] == t["id"])

    async def recorded() -> bool:
        return (await mine())["last_sent_at"] is not None

    await until(recorded, within=10)
    r = await c.patch(f"/notifications/targets/{t['id']}", json={"name": "Renamed"},
                      headers={"If-Match": tag})  # fmt: skip
    assert r.status_code == 200, r.text
    assert (await c.post(f"/notifications/targets/{t['id']}/test")).json()["ok"]
    r = await c.patch(f"/notifications/targets/{t['id']}", json={"name": "Again"},
                      headers={"If-Match": r.json()["etag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    got = await mine()
    assert got["name"] == "Again" and got["last_error"] is None


async def test_discord_message_and_errors(c: httpx.AsyncClient, server: Running) -> None:
    sent: list[dict[str, Any]] = []
    status = [204]

    def handle(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == DISCORD
        sent.append(json.loads(req.content))
        return httpx.Response(
            status[0], json={"message": "Unknown Webhook"} if status[0] >= 400 else None
        )

    server.app.state.notifications.provider_kwargs = {"transport": httpx.MockTransport(handle)}
    r = await c.post("/notifications/targets", json={"kind": "discord", "name": "Discord",
                     "settings": {"webhook_url": "https://example.com/hook"}})  # fmt: skip
    assert r.status_code == 422 and "Discord webhook URL" in r.text
    r = await c.post("/notifications/targets", json={"kind": "discord", "name": "Discord",
                     "settings": {"webhook_url": f" {DISCORD}\n"}})  # fmt: skip
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert (await c.post(f"/notifications/targets/{tid}/test")).json()["ok"]
    [msg] = sent
    assert msg["username"] == "Fetchbox" and msg["allowed_mentions"] == {"parse": []}
    assert msg["embeds"][0]["title"] == "Fetchbox test" and msg["embeds"][0]["color"] == 0x16A34A

    status[0] = 404  # the webhook was deleted in Discord
    await server.app.state.fb.bus.emit("storage.offline", name="NAS movies", path="/mnt/nas",
                                       message="/mnt/nas isn't mounted")  # fmt: skip

    async def failed() -> bool:
        rows = (await c.get("/notifications/targets")).json()
        return bool(rows[0]["last_error"])

    await until(failed, within=10)
    err = (await c.get("/notifications/targets")).json()[0]["last_error"]
    assert err.startswith("HTTP 404") and "Unknown Webhook" in err


def test_discord_embed_limits() -> None:
    n = Notification(event="import.completed", title="T" * 400, body="B" * 5000,
                     level="warning", occurred_at=utcnow())  # fmt: skip
    e = embed(n)["embeds"][0]  # type: ignore[index]
    assert len(e["title"]) == 256 and len(e["description"]) == 4096 and e["color"] == 0xD97706


async def test_block_private_webhook_addresses(
    c: httpx.AsyncClient, server: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fetchbox.core import netguard

    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(204)

    async def resolve(host: str, port: int) -> list[str]:
        return {"hooks.example": ["93.184.216.34"], "rebind.example": ["93.184.216.34", "10.0.0.7"],
                "home.example": ["192.168.1.20"]}[host]  # fmt: skip

    monkeypatch.setattr(netguard, "_resolve", resolve)
    server.app.state.notifications.provider_kwargs = {"transport": httpx.MockTransport(handle)}

    async def target(url: str) -> str:
        r = await c.post("/notifications/targets", json={"kind": "webhook", "name": url,
                         "settings": {"url": url}})  # fmt: skip
        assert r.status_code == 201, r.text
        return str(r.json()["id"])

    home = await target("http://home.example:8123/api/webhook/x")
    public = await target("https://hooks.example/in")
    # off by default: a webhook to the home network is fine
    assert (await c.post(f"/notifications/targets/{home}/test")).json()["ok"]
    assert seen[-1].url.host == "home.example"

    await c.patch("/system/config", json={"network": {"block_private_webhooks": True}})
    for url in ("http://home.example:8123/x", "http://127.0.0.1:9/x", "http://[::1]/x",
                "http://169.254.169.254/latest", "http://rebind.example/x"):  # fmt: skip
        res = (await c.post(f"/notifications/targets/{await target(url)}/test")).json()
        assert not res["ok"] and "private or local address" in res["message"], (url, res)
    n = len(seen)
    assert (await c.post(f"/notifications/targets/{home}/test")).json()["ok"] is False
    assert len(seen) == n  # nothing was sent

    # a public one goes to the address that was checked, under its own name
    assert (await c.post(f"/notifications/targets/{public}/test")).json()["ok"]
    req = seen[-1]
    assert req.url.host == "93.184.216.34" and req.headers["host"] == "hooks.example"
    assert req.extensions.get("sni_hostname") == "hooks.example"
