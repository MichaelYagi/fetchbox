"""A plugin that keeps failing is paused for an hour (its events aren't queued, its jobs
skip) and says so; it resumes on its own. Diagnostics list the plugins, never settings."""

from __future__ import annotations

import io
import json
import time
import zipfile
from typing import Any, ClassVar

import httpx

from fetchbox.plugin import host as host_mod
from fetchbox_plugin import Addon, Plugin, PublicEvent, every, on
from fetchbox_plugin.testing import fetchbox_app  # noqa: F401

CALLS: list[str] = []


class Flaky(Addon):
    id = "flaky"
    name = "Flaky"

    @on("title.added")
    async def added(self, ctx: Any, event: PublicEvent) -> None:
        CALLS.append("added")
        raise RuntimeError("the service is down")

    @every(hours=1)
    async def tidy(self, ctx: Any) -> dict[str, Any]:
        CALLS.append("tidy")
        return {"tidied": True}


class FlakyPlugin(Plugin):
    id = "flaky"
    name = "Flaky"
    contributes: ClassVar[list[type[Any]]] = [Flaky]


async def test_failing_repeatedly_pauses_it(fetchbox_app: Any) -> None:
    CALLS.clear()
    app = await fetchbox_app(plugins=[FlakyPlugin])
    got = app.host.loaded["flaky"]
    for _ in range(host_mod.PAUSE_AFTER):
        [ran] = await app.emit("title.added", kind="movie", check=False)
        assert ran.status == "failed" and "the service is down" in (ran.error or "")
    assert got.paused() and got.last_error == "RuntimeError: the service is down"
    assert await app.emit("title.added", kind="movie") == []  # not even queued
    ran = await app.run("flaky", "tidy")
    assert ran.result == {"skipped": "Flaky is paused after failing repeatedly"}
    assert CALLS.count("added") == host_mod.PAUSE_AFTER and "tidy" not in CALLS

    got.paused_until = time.time() - 1  # an hour later
    assert not got.paused() and got.failures == []
    assert (await app.run("flaky", "tidy")).result == {"tidied": True}


async def test_a_success_resets_the_count(fetchbox_app: Any) -> None:
    app = await fetchbox_app(plugins=[FlakyPlugin])
    got = app.host.loaded["flaky"]
    for _ in range(host_mod.PAUSE_AFTER - 1):
        await app.emit("title.added", kind="movie", check=False)
    await app.run("flaky", "tidy")  # works: the failures before don't count any more
    await app.emit("title.added", kind="movie", check=False)
    assert not got.paused()


async def test_diagnostics_list_plugins_not_their_settings(
    c: httpx.AsyncClient, server: Any
) -> None:
    mgr = server.app.state.plugins
    await mgr._set("flaky", approved=True, enabled=True, settings={"flaky": {"token": "s3cret"}})
    r = await c.get("/system/diagnostics")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    [p] = json.loads(z.read("plugins.json"))
    assert (p["id"], p["approved"], p["enabled"]) == ("flaky", True, True)
    assert "settings" not in p and "s3cret" not in z.read("plugins.json").decode()
