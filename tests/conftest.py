from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from fetchbox.app import create_app
from fetchbox.config import Settings, load_settings


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for var in ("FETCHBOX_DATA_DIR", "FETCHBOX_PORT", "FETCHBOX_DB_URL", "FETCHBOX_HOST"):
        monkeypatch.delenv(var, raising=False)
    return load_settings(tmp_path / "data")


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app  # type: ignore[attr-defined]
            yield c
