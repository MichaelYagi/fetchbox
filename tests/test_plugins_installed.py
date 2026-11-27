"""Installed plugins through the API: a wheel installed, approved and running; its settings
kept encrypted and shown masked; on and off; loaded again when Fetchbox restarts (unless
--no-plugins); found but never approved stays off; removed."""

from __future__ import annotations

import io
import json
import os
import sys
import textwrap
import time
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from fetchbox.config import Settings
from fetchbox.core.secrets import MASK
from tests.conftest import Running, running

MODULE = "fbtest_greeter"

CODE = """
from fetchbox_plugin import (
    Addon, Field, Panel, Permission, Plugin, PublicEvent, Secret, Settings, action, on, panel,
)


class GreeterSettings(Settings):
    word: str = Field("Hello", title="Word")
    token: Secret = Field({token_default}, title="Token")

    async def test(self, ctx):
        if self.token.value != "s3cret":
            raise ValueError("the token is wrong")
        return "The token works"


class Greeter(Addon):
    id = "greeter"
    name = "Greeter"
    settings = GreeterSettings

    @on("title.added")
    async def added(self, ctx, event: PublicEvent):
        return {{"said": self.settings.word}}

    @panel(on="title", title="Greetings")
    async def greetings(self, ctx, title_id):
        if title_id == "boom":
            raise RuntimeError("no greetings today")
        return Panel(status=f"{{self.settings.word}} there", table=[{{"Who": "you"}}],
                     actions=["Greet"])

    @action("Greet", on="title")
    async def greet(self, ctx, item):
        await ctx.store.set("greeted", [item, self.settings.word, self.settings.token.value])
        return {{"greeted": item}}


class GreeterPlugin(Plugin):
    id = "greeter"
    name = "Greeter"
    description = "Says hello."
    permissions = {{Permission.NOTIFY}}
    contributes = [Greeter]
"""


def wheel(*, required_token: bool = False, extra: dict[str, bytes] | None = None,
          entry: bool = True, version: str = "0.1.0") -> bytes:  # fmt: skip
    code = CODE.format(token_default="..." if required_token else '""')
    files = {
        f"{MODULE}/__init__.py": textwrap.dedent(code).encode(),
        f"{MODULE}-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {MODULE}\nVersion: {version}\n".encode(),  # noqa: E501
        f"{MODULE}-{version}.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",  # noqa: E501
        **(extra or {}),
    }
    if entry:
        files[f"{MODULE}-{version}.dist-info/entry_points.txt"] = (
            f"[fetchbox.plugins]\ngreeter = {MODULE}:GreeterPlugin\n".encode()
        )
    files[f"{MODULE}-{version}.dist-info/RECORD"] = "\n".join(
        f"{n},," for n in [*files, f"{MODULE}-{version}.dist-info/RECORD"]
    ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@asynccontextmanager
async def restart(settings: Settings, key: str) -> AsyncIterator[Running]:
    """Fetchbox started again on the same data folder (no new bootstrap key then)."""
    from asgi_lifespan import LifespanManager

    from fetchbox.app import create_app

    app = create_app(settings, on_bootstrap_key=lambda _k: None)
    async with LifespanManager(app, startup_timeout=60, shutdown_timeout=60):
        yield Running(app, key)


@pytest.fixture(autouse=True)
def _clean_imports() -> Iterator[None]:
    """Each test installs the wheel afresh: forget the module and the plugins folder."""
    yield
    for m in [m for m in sys.modules if m == MODULE or m.startswith(MODULE + ".")]:
        del sys.modules[m]
    sys.path[:] = [p for p in sys.path if not p.endswith(str(Path("plugins") / "lib"))]


async def _install(c: Any, data: bytes, name: str = f"{MODULE}-0.1.0-py3-none-any.whl") -> Any:
    return await c.post(
        "/plugins/install", files={"file": (name, data, "application/octet-stream")}
    )


async def test_install_configure_and_run(settings: Settings) -> None:
    async with running(settings) as server, server.client() as c:
        assert (await c.get("/plugins")).json() == []  # nothing installed: nothing listed
        r = await _install(c, wheel())
        assert r.status_code == 201, r.text
        p = r.json()
        assert (p["id"], p["approved"], p["enabled"], p["running"], p["error"]) == (
            "greeter",
            True,
            True,
            True,
            None,
        )
        assert p["permissions"] == [
            {"id": "notify", "words": "Sends messages through your notification targets"}
        ]
        assert p["actions"] == [
            {
                "addon": "greeter",
                "method": "greet",
                "label": "Greet",
                "on": "title",
                "kinds": None,
                "needs_file": False,
            }
        ]
        [s] = p["settings"]
        assert s["schema"]["properties"]["token"]["writeOnly"] is True and s["has_test"]
        assert s["values"] == {"token": None} or s["values"].get("token") is None

        # settings: kept encrypted, shown masked, given to the plugin as they are
        r = await c.put(
            "/plugins/greeter/settings/greeter", json={"values": {"word": "Hi", "token": "s3cret"}}
        )
        assert r.status_code == 200, r.text
        assert r.json()["settings"][0]["values"] == {"word": "Hi", "token": MASK}
        stored = server.app.state.plugins.records["greeter"].settings["greeter"]
        assert stored["word"] == "Hi" and "s3cret" not in json.dumps(stored)
        r = await c.put(
            "/plugins/greeter/settings/greeter", json={"values": {"word": "Hey", "token": MASK}}
        )
        assert r.status_code == 200  # MASK back: the token is kept
        job = (await c.post("/plugins/greeter/actions/greeter/greet", json={"item": "t1"})).json()[
            "job_id"
        ]
        assert job
        ctx = server.app.state.plugins.host.loaded["greeter"].ctx

        async def greeted() -> bool:
            return await ctx.store.get("greeted") == ["t1", "Hey", "s3cret"]

        from tests.torrents import until

        await until(greeted, within=15)
        bad = await c.put("/plugins/greeter/settings/greeter", json={"values": {"colour": "red"}})
        assert bad.status_code == 422

        # the Test button, with what's typed (not saved)
        t = await c.post(
            "/plugins/greeter/settings/greeter/test", json={"values": {"token": "nope"}}
        )
        assert t.json() == {"ok": False, "message": "the token is wrong"}
        t = await c.post("/plugins/greeter/settings/greeter/test", json={"values": {"token": MASK}})
        assert t.json() == {"ok": True, "message": "The token works"}

        # off and on
        r = await c.patch("/plugins/greeter", json={"enabled": False})
        assert (r.json()["enabled"], r.json()["running"]) == (False, False)
        r = await c.patch("/plugins/greeter", json={"enabled": True})
        assert r.json()["running"] is True
        key = server.root_key

    # Fetchbox starts again: it runs again, with its settings
    async with restart(settings, key) as server, server.client() as c:
        p = (await c.get("/plugins/greeter")).json()
        assert p["running"] is True and p["settings"][0]["values"]["word"] == "Hey"

    # --no-plugins: everything off, for when one stops Fetchbox from starting
    off = settings.model_copy(update={"no_plugins": True})
    async with restart(off, key) as server, server.client() as c:
        p = (await c.get("/plugins/greeter")).json()
        assert (p["enabled"], p["running"]) == (True, False)


async def test_required_settings_wait(settings: Settings) -> None:
    async with running(settings) as server, server.client() as c:
        p = (await _install(c, wheel(required_token=True))).json()
        assert p["running"] is False and "settings aren't complete" in p["error"]
        assert p["settings"][0]["missing"] == ["token"]
        r = await c.put("/plugins/greeter/settings/greeter", json={"values": {"token": "s3cret"}})
        assert (r.json()["running"], r.json()["error"], r.json()["settings"][0]["missing"]) == (
            True,
            None,
            [],
        )


async def test_found_but_never_approved_stays_off(settings: Settings) -> None:
    lib = settings.data_dir / "plugins" / "lib"
    lib.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(wheel())) as z:
        z.extractall(lib)  # copied in by hand, not installed from Settings
    async with running(settings) as server, server.client() as c:
        p = (await c.get("/plugins/greeter")).json()
        assert (p["approved"], p["enabled"], p["running"]) == (False, False, False)
        assert (await c.patch("/plugins/greeter", json={"enabled": True})).json()["running"] is True


async def test_remove(settings: Settings) -> None:
    async with running(settings) as server, server.client() as c:
        await _install(c, wheel())
        await c.put("/plugins/greeter/settings/greeter", json={"values": {"word": "Hi"}})
        assert (
            await c.delete("/plugins/greeter", params={"keep_settings": "true"})
        ).status_code == 204
        p = (await c.get("/plugins/greeter")).json()
        assert p["running"] is False and p["error"] == "removed"
        assert not (settings.data_dir / "plugins" / "lib" / MODULE).exists()
        assert (await c.delete("/plugins/greeter")).status_code == 204
        assert (await c.get("/plugins/greeter")).status_code == 404


@pytest.mark.parametrize(
    ("data", "name", "why"),
    [
        (b"not a zip", "x.whl", "isn't a wheel"),
        (lambda: wheel(), "x.zip", "comes as a .whl"),
        (lambda: wheel(entry=False), "x.whl", "isn't a Fetchbox plugin"),
        (lambda: wheel(extra={f"{MODULE}/_speed.so": b"\0"}), "x.whl", "native code"),
        (lambda: wheel(extra={"../evil.py": b""}), "x.whl", "outside its folder"),
    ],
)
async def test_what_isnt_installed(settings: Settings, data: Any, name: str, why: str) -> None:
    async with running(settings) as server, server.client() as c:
        r = await _install(c, data() if callable(data) else data, name)
        assert r.status_code == 409 and why in r.json()["detail"], r.text
        assert (await c.get("/plugins")).json() == []


async def test_a_real_plugin_wheel(settings: Settings, tmp_path: Path) -> None:
    """The Subtitles reference plugin as `uv build` makes it, installed from Settings: it
    waits for its API key, then runs."""
    import shutil
    import subprocess

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv isn't installed")
    project = Path(__file__).parents[1] / "plugins" / "fetchbox-subtitles"
    import asyncio

    cmd = [uv, "build", "--wheel", "-q", "-o", str(tmp_path), str(project)]
    await asyncio.to_thread(subprocess.run, cmd, check=True)
    [whl] = tmp_path.glob("*.whl")
    async with running(settings) as server, server.client() as c:
        r = await _install(c, whl.read_bytes(), whl.name)
        assert r.status_code == 201, r.text
        p = r.json()
        import tomllib

        version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
        assert (p["id"], p["version"], p["running"]) == ("subtitles", version, False)
        assert p["settings"][0]["missing"] == ["api_key"]
        r = await c.put("/plugins/subtitles/settings/subtitles", json={"values": {"api_key": "k"}})
        assert r.json()["running"] is True
        # its buttons say which titles they're for: movies and shows (not comics)
        acts = r.json()["actions"]
        assert acts and {a["kinds"] == ["movie", "series"] for a in acts} == {True}
        await c.delete("/plugins/subtitles")
    for m in [m for m in sys.modules if m.startswith("fetchbox_subtitles")]:
        del sys.modules[m]


async def test_panels_messages_and_health(server: Any, c: Any) -> None:
    """With a plugin running: its panel on a title page, plugin messages offered to
    notification targets, and its line in Health. None of it without a running plugin."""
    from tests.pipeline import _movie

    events = {e["event"] for e in (await c.get("/notifications/events")).json()}
    assert "plugin.message" not in events  # no plugin: nothing new
    health = (await c.get("/system/health")).json()
    assert "plugins" not in json.dumps(health)

    assert (await _install(c, wheel())).status_code == 201
    movie = await _movie(c)
    [p] = (await c.get("/plugins/panels", params={"on": "title", "id": movie["id"]})).json()
    assert (p["plugin"], p["title"], p["status"], p["table"], p["actions"], p["error"]) == (
        "greeter",
        "Greetings",
        "Hello there",
        [{"Who": "you"}],
        ["Greet"],
        None,
    )
    assert (await c.get("/plugins/panels", params={"on": "title", "id": "nope"})).json() == []

    events = {e["event"]: e for e in (await c.get("/notifications/events")).json()}
    assert events["plugin.message"]["default"] is False
    health = json.dumps((await c.get("/system/health")).json())
    assert "1 plugin running" in health  # (its link to Settings shows when not ok)

    await c.patch("/plugins/greeter", json={"enabled": False})
    assert "plugin.message" not in {
        e["event"] for e in (await c.get("/notifications/events")).json()
    }


def _fake_web(data: bytes) -> tuple[httpx.MockTransport, list[str]]:
    seen: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "pypi.org" and req.url.path == f"/pypi/{MODULE}/json":
            whl = f"https://files.example/{MODULE}-0.1.0-py3-none-any.whl"
            return httpx.Response(200, json={"urls": [
                {"packagetype": "sdist", "filename": f"{MODULE}-0.1.0.tar.gz", "url": "x"},
                {"packagetype": "bdist_wheel", "filename": f"{MODULE}-0.1.0-py3-none-any.whl", "url": whl},  # noqa: E501
            ]})  # fmt: skip
        if req.url.path.endswith(".whl"):
            return httpx.Response(200, content=data)
        return httpx.Response(404)

    return httpx.MockTransport(handle), seen


async def test_install_from_an_address(settings: Settings) -> None:
    async with running(settings) as server:
        mgr = server.app.state.plugins
        transport, _seen = _fake_web(wheel())
        pid = await mgr.install_url(
            f"https://files.example/{MODULE}-0.1.0-py3-none-any.whl", transport=transport
        )
        assert pid == "greeter" and "greeter" in mgr.host.loaded
        from fetchbox.plugin.manager import PluginError

        with pytest.raises(PluginError, match="starts with https"):
            await mgr.install_url("ftp://x/y.whl")
        with pytest.raises(PluginError, match="answered 404"):
            await mgr.install_url("https://files.example/missing", transport=transport)


async def test_plugins_named_in_the_environment(settings: Settings) -> None:
    """FETCHBOX_PLUGINS (Docker): installed from PyPI once, then kept."""
    transport, seen = _fake_web(wheel())
    async with running(settings) as server:
        mgr = server.app.state.plugins
        assert await mgr.install_from_env(MODULE, transport=transport) == ["greeter"]
        assert mgr.records["greeter"].approved and "greeter" in mgr.host.loaded
        n = len(seen)
        assert await mgr.install_from_env(MODULE, transport=transport) == []  # there already
        assert len(seen) == n
        assert await mgr.install_from_env("nonexistent-plugin", transport=transport) == []


WHL = f"{MODULE}-0.1.0-py3-none-any.whl"


async def test_a_wheel_in_the_drop_folder(settings: Settings) -> None:
    """Copied into <data>/plugins/drop: installed, but not approved: nothing runs until it's
    turned on. Then out of the way, into drop/installed/."""
    async with running(settings) as server:
        mgr = server.app.state.plugins
        drop = settings.data_dir / "plugins" / "drop"
        assert drop.is_dir()  # made at start, for people to find
        (drop / WHL).write_bytes(wheel())
        assert await mgr.scan_drop(settle=0) == ["greeter"]
        r = mgr.records["greeter"]
        assert (r.approved, r.enabled) == (False, False) and "greeter" not in mgr.host.loaded
        assert not (drop / WHL).exists() and (drop / "installed" / WHL).exists()
        assert await mgr.scan_drop(settle=0) == []  # not installed twice

        # turned on, then a newer version dropped: upgraded, and still on
        await mgr.set_enabled("greeter", True)
        assert "greeter" in mgr.host.loaded
        (drop / f"{MODULE}-0.2.0-py3-none-any.whl").write_bytes(wheel(version="0.2.0"))
        assert await mgr.scan_drop(settle=0) == ["greeter"]
        r = mgr.records["greeter"]
        assert (r.approved, r.enabled, r.version) == (True, True, "0.2.0")
        assert "greeter" in mgr.host.loaded


async def test_the_drop_folder_waits_for_a_copy_to_finish(settings: Settings) -> None:
    async with running(settings) as server:
        mgr = server.app.state.plugins
        f = settings.data_dir / "plugins" / "drop" / WHL
        f.write_bytes(wheel())
        assert await mgr.scan_drop() == []  # just arrived: maybe still copying
        old = time.time() - 60
        os.utime(f, (old, old))
        assert await mgr.scan_drop() == []  # seen once like this: one more look
        assert await mgr.scan_drop() == ["greeter"]  # unchanged since: installed


async def test_a_bad_file_in_the_drop_folder_says_why(settings: Settings) -> None:
    async with running(settings) as server:
        mgr = server.app.state.plugins
        drop = settings.data_dir / "plugins" / "drop"
        (drop / "notaplugin.whl").write_bytes(b"not a zip")
        assert await mgr.scan_drop(settle=0) == []
        assert (drop / "failed" / "notaplugin.whl").exists()
        health = await mgr.health()
        assert health.status == "warning" and "notaplugin.whl in the drop folder" in health.message


async def test_dropped_while_fetchbox_was_off(settings: Settings) -> None:
    drop = settings.data_dir / "plugins" / "drop"
    drop.mkdir(parents=True)
    (drop / WHL).write_bytes(wheel())
    async with running(settings) as server:
        r = server.app.state.plugins.records["greeter"]
        assert (r.approved, r.enabled) == (False, False)
        assert (drop / "installed" / WHL).exists()


async def test_an_upgrade_leaves_one_version_installed(settings: Settings) -> None:
    """A newer wheel replaces the older one's record too, so the version is the new one on
    every system (both records left there, either could be read)."""
    async with running(settings) as server:
        mgr = server.app.state.plugins
        await mgr.install_wheel(wheel(), WHL)
        await mgr.install_wheel(wheel(version="0.2.0"), f"{MODULE}-0.2.0-py3-none-any.whl")
        lib = settings.data_dir / "plugins" / "lib"
        assert sorted(p.name for p in lib.glob("*.dist-info")) == [f"{MODULE}-0.2.0.dist-info"]
        assert mgr.records["greeter"].version == "0.2.0"
