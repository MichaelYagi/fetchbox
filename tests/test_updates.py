"""Update check: a newer release is offered; off means no contact at all."""

from __future__ import annotations

import httpx

from fetchbox.modules.system.updates import UpdateChecker, newer
from tests.conftest import Running


def test_version_comparison() -> None:
    assert newer("v0.2.0", "0.1.0") and newer("v0.1.1", "0.1.0.dev0")
    assert not newer("v0.1.0", "0.1.0.dev0")  # a dev build of 0.1.0 isn't offered 0.1.0
    assert not newer("v0.1.0", "0.2.0") and not newer("nightly", "0.1.0")


async def test_updates_endpoint(server: Running) -> None:
    calls: list[str] = []

    def github(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        page = "https://github.com/MichaelYagi/fetchbox/releases/tag/v9.0.0"
        body = {"tag_name": "v9.0.0", "published_at": "2027-01-01", "html_url": page}
        return httpx.Response(200, json=body)

    server.app.state.updates = UpdateChecker(httpx.MockTransport(github))
    async with server.client() as c:
        r = (await c.get("/system/updates")).json()
        assert r["update_available"] and r["latest"] == "v9.0.0" and r["checked"]
        await c.get("/system/updates")
        assert len(calls) == 1  # cached
        assert (
            await c.patch("/system/config", json={"updates": {"check": False}})
        ).status_code == 200
        r = (await c.get("/system/updates", params={"fresh": True})).json()
        assert not r["checked"] and not r["update_available"] and len(calls) == 1  # no contact
