from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from fetchbox.app import create_app
from fetchbox.config import Settings, load_settings
from fetchbox.core.jobs import JobRunner

pytest_plugins = ["tests.pipeline"]


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for var in ("FETCHBOX_DATA_DIR", "FETCHBOX_PORT", "FETCHBOX_DB_URL", "FETCHBOX_HOST"):
        monkeypatch.delenv(var, raising=False)
    # Never touch the real Downloads folder or the public torrent network from tests.
    monkeypatch.setenv("FETCHBOX_DOWNLOADS_ROOT", str(tmp_path / "Downloads" / "Fetchbox"))
    monkeypatch.setenv("FETCHBOX_TORRENT_ISOLATED", "1")
    monkeypatch.setenv("FETCHBOX_SEED_INDEXERS", "0")  # tests never reach real sites
    return load_settings(tmp_path / "data")


@dataclass
class Running:
    app: FastAPI
    root_key: str  # the bootstrap `default` key with scope *

    def client(self, key: str | None = None, **kw: object) -> httpx.AsyncClient:
        """Client for /api/v1. `key=None` uses the root key; `key=""` sends no credentials."""
        headers = {} if key == "" else {"Authorization": f"Bearer {key or self.root_key}"}
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test/api/v1",
            headers=headers,
            **kw,  # type: ignore[arg-type]
        )


@asynccontextmanager
async def running(
    settings: Settings, hooks: Sequence[Callable[[JobRunner], None]] = ()
) -> AsyncIterator[Running]:
    keys: list[str] = []
    app = create_app(settings, job_handler_hooks=hooks, on_bootstrap_key=keys.append)
    # Generous: slow CI runners (Windows) need more than the 5 s default to start the engine.
    async with LifespanManager(app, startup_timeout=60, shutdown_timeout=60):
        assert keys, "bootstrap key was not issued"
        yield Running(app, keys[0])


@pytest.fixture
async def server(settings: Settings) -> AsyncIterator[Running]:
    async with running(settings) as r:
        yield r


@pytest.fixture
async def client(server: Running) -> AsyncIterator[httpx.AsyncClient]:
    async with server.client() as c:
        yield c
