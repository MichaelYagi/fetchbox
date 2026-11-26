"""``fetchbox plugin dev <path>``: Fetchbox running with a plugin loaded from its source, which
is loaded again each time a file of it is saved.

A throwaway data folder (or ``--data`` to keep one), offline like the test kit (no torrent
network, no indexers), a sample movie in the library to try an add-on on, and the browser
opened at it. Ctrl+C stops it.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import sys
import tempfile
import tomllib
import webbrowser
from pathlib import Path
from typing import Any

from fetchbox.plugin.host import PluginHost
from fetchbox_plugin import Plugin

POLL_S = 1.0


class DevError(Exception):
    pass


class DevSession:
    """One plugin project, loaded into a host, reloaded when its files change."""

    def __init__(self, host: PluginHost, project: Path) -> None:
        self.host = host
        self.project = project.resolve()
        module, attr = _entry_point(self.project)
        self.module, self.attr = module, attr
        self.top = module.split(".")[0]
        src = self.project / "src"
        self.path = src if (src / self.top).exists() else self.project
        self.plugin_id: str | None = None
        self.stamp = self._stamp()

    def _files(self) -> list[Path]:
        pkg = self.path / self.top
        return sorted(pkg.rglob("*.py")) if pkg.is_dir() else [pkg.with_suffix(".py")]

    def _stamp(self) -> dict[Path, int]:
        out = {}
        for f in self._files():
            with contextlib.suppress(OSError):  # deleted while we looked
                out[f] = f.stat().st_mtime_ns
        return out

    def _import(self) -> type[Plugin]:
        for name in [m for m in sys.modules if m == self.top or m.startswith(self.top + ".")]:
            del sys.modules[name]  # a fresh import: the code as it is now
        importlib.invalidate_caches()
        if str(self.path) not in sys.path:
            sys.path.insert(0, str(self.path))
        cls = getattr(importlib.import_module(self.module), self.attr, None)
        if not (isinstance(cls, type) and issubclass(cls, Plugin)):
            raise DevError(f"{self.module}:{self.attr} isn't a Plugin class")
        return cls

    async def load(self) -> str:
        cls = self._import()
        await self.host.load(cls)
        self.plugin_id = cls.id
        return cls.id

    async def check(self) -> str | None:
        """Reload when a file changed: what happened (None when nothing changed)."""
        now = await asyncio.to_thread(self._stamp)
        if now == self.stamp:
            return None
        self.stamp = now
        if self.plugin_id is not None:
            await self.host.unload(self.plugin_id)
            self.plugin_id = None
        try:
            pid = await self.load()
        except Exception as exc:  # the developer's code: say what's wrong, keep running
            return f"not loaded: {type(exc).__name__}: {exc} (fix it and save again)"
        return f"reloaded {pid}"


def _entry_point(project: Path) -> tuple[str, str]:
    try:
        meta = tomllib.loads((project / "pyproject.toml").read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DevError(f"no readable pyproject.toml in {project}: {exc}") from None
    eps = ((meta.get("project") or {}).get("entry-points") or {}).get("fetchbox.plugins") or {}
    if len(eps) != 1:
        raise DevError("pyproject.toml needs one fetchbox.plugins entry point (plugin check)")
    module, _, attr = str(next(iter(eps.values()))).partition(":")
    return module.strip(), attr.strip()


async def run(project: Path, *, port: int, data: Path | None, browser: bool) -> int:
    import uvicorn

    from fetchbox.app import create_app
    from fetchbox.config import load_settings
    from fetchbox.plugin.services import Services
    from fetchbox.testing import App, _isolated

    root = data or Path(tempfile.mkdtemp(prefix="fetchbox-dev-"))
    with _isolated(root):
        os.environ["FETCHBOX_PORT"] = str(port)  # (restored with the rest on the way out)
        keys: list[str] = []
        fastapi = create_app(load_settings(root / "data"), on_bootstrap_key=keys.append)
        server = uvicorn.Server(
            uvicorn.Config(fastapi, host="127.0.0.1", port=port, log_level="warning")
        )
        serving = asyncio.create_task(server.serve())
        while not server.started:
            if serving.done():
                return 1  # couldn't start (port taken: uvicorn said why)
            await asyncio.sleep(0.05)
        st = fastapi.state
        services = Services(
            library=st.library,
            media=st.media,
            jobs=st.fb.jobs,
            notifications=st.notifications,
            autosearch=st.autosearch,
        )
        host = PluginHost(db=st.fb.db, bus=st.fb.bus, scheduler=st.fb.scheduler,
                          services=services, data_dir=root / "data")  # fmt: skip
        session = DevSession(host, project)
        url = f"http://127.0.0.1:{port}"
        try:
            pid = await session.load()
            print(f"{pid} loaded from {session.path}", flush=True)
        except Exception as exc:
            print(f"not loaded yet: {type(exc).__name__}: {exc} (fix it and save)", flush=True)
        sample = App(fastapi, host, _no_client(), root)
        try:
            await sample.add_file("Dune", 2021, imported=False)
        except Exception as exc:  # a library folder the sample can't use: not essential
            print(f"(no sample movie: {exc})", flush=True)
        print(f"Fetchbox: {url}  (data: {root})", flush=True)
        if keys:
            print(f"API key: {keys[0]}  (the web page asks you to create an account)", flush=True)
        print("Saving a file of the plugin loads it again. Ctrl+C stops.", flush=True)
        if browser:
            webbrowser.open(url)
        try:
            while not serving.done():
                await asyncio.sleep(POLL_S)
                if (msg := await session.check()) is not None:
                    print(msg, flush=True)
        finally:
            if session.plugin_id:
                await host.unload(session.plugin_id)
            server.should_exit = True
            await serving
    return 0


def _no_client() -> Any:
    """App's client isn't used by add_file; the dev server has the real one."""
    return None
