"""A plugin's provider (here a notifier): offered like the built-in ones while the plugin
runs, used for real, and gone when it's turned off."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
from pydantic import BaseModel

from fetchbox.core.providers import TestResult
from fetchbox.providers.notifiers import Notification, NotifierProvider
from fetchbox_plugin import Plugin
from tests.conftest import Running
from tests.torrents import until

SENT: list[Notification] = []


class Pigeon(NotifierProvider):
    kind = "pigeon"

    class Settings(BaseModel):
        loft: str = "home"

    async def send(self, n: Notification) -> None:
        SENT.append(n)

    async def test(self) -> TestResult:
        return TestResult(ok=True, message="the pigeon is ready")


class PigeonPlugin(Plugin):
    id = "pigeon"
    name = "Pigeon post"
    contributes: ClassVar[list[type[Any]]] = [Pigeon]


async def test_a_plugins_notifier(server: Running, c: httpx.AsyncClient) -> None:
    SENT.clear()
    assert "pigeon" not in (await c.get("/notifications/kinds")).json()
    # its target is still listed (Settings > Notifications keeps working), and isn't used
    r = await c.get("/notifications/targets")
    assert r.status_code == 200, r.text
    SENT.clear()
    await server.app.state.fb.bus.emit("download.failed", name="Dune", error="again")
    await server.app.state.notifications.stop()
    assert SENT == []
    host = server.app.state.plugins.host
    await host.load(PigeonPlugin)
    try:
        assert "pigeon" in (await c.get("/notifications/kinds")).json()
        r = await c.post("/notifications/targets", json={"kind": "pigeon", "name": "Loft",
                         "settings": {"loft": "roof"}, "events": ["download.failed"]})  # fmt: skip
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
        assert (await c.post(f"/notifications/targets/{tid}/test")).json()["ok"]
        await server.app.state.fb.bus.emit(
            "download.failed", name="Dune", error="the tracker is down"
        )

        async def sent() -> bool:
            return any(n.event == "download.failed" for n in SENT)

        await until(sent, within=10)
    finally:
        await host.unload("pigeon")
    assert "pigeon" not in (await c.get("/notifications/kinds")).json()
    # its target is still listed (Settings > Notifications keeps working), and isn't used
    r = await c.get("/notifications/targets")
    assert r.status_code == 200, r.text
    SENT.clear()
    await server.app.state.fb.bus.emit("download.failed", name="Dune", error="again")
    await server.app.state.notifications.stop()
    assert SENT == []
