"""Installed plugins through the API: a wheel installed, approved and running; its settings
kept encrypted and shown masked; on and off; loaded again when Fetchbox restarts (unless
--no-plugins); found but never approved stays off; removed."""

from __future__ import annotations

import io
import json
import sys
import textwrap
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from fetchbox.config import Settings
from fetchbox.core.secrets import MASK
from tests.conftest import Running, running

MODULE = "fbtest_greeter"

CODE = """
from fetchbox_plugin import (
    Addon, Field, Permission, Plugin, PublicEvent, Secret, Settings, action, on,
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
            {"addon": "greeter", "method": "greet", "label": "Greet", "on": "title"}
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
        assert (p["id"], p["version"], p["running"]) == ("subtitles", "0.1.0", False)
        assert p["settings"][0]["missing"] == ["api_key"]
        r = await c.put("/plugins/subtitles/settings/subtitles", json={"values": {"api_key": "k"}})
        assert r.json()["running"] is True
        await c.delete("/plugins/subtitles")
    for m in [m for m in sys.modules if m.startswith("fetchbox_subtitles")]:
        del sys.modules[m]
