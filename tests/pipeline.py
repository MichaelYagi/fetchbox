"""Shared fixtures for tests that run the search pipeline against fake TMDB + Torznab.

Registered through ``pytest_plugins`` in conftest, so tests just name ``fakes`` / ``c``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from tests.conftest import Running
from tests.fake_tmdb import V3_KEY, FakeTmdb
from tests.fake_torznab import API_KEY, GB, FakeTorznab, Item


class Fakes:
    def __init__(self) -> None:
        self.tmdb = FakeTmdb()
        self.torznab: dict[str, FakeTorznab] = {}

    def handle(self, req: httpx.Request) -> httpx.Response:
        return self.torznab[req.url.host].handle(req)


@pytest.fixture
async def fakes(server: Running) -> Fakes:
    f = Fakes()
    server.app.state.discovery.transport = f.tmdb.transport
    server.app.state.sources.provider_kwargs = {"transport": httpx.MockTransport(f.handle)}
    async with server.client() as c:
        await c.patch("/system/config", json={"tmdb": {"api_key": V3_KEY}})
    return f


@pytest.fixture
async def c(server: Running, fakes: Fakes) -> AsyncIterator[httpx.AsyncClient]:
    async with server.client() as client:
        yield client


async def _indexer(
    c: httpx.AsyncClient, fakes: Fakes, host: str, items: list[Item], **kw: Any
) -> dict[str, Any]:
    fakes.torznab[host] = FakeTorznab(items)
    body = {"name": host, "settings": {"url": f"http://{host}", "api_key": API_KEY}, **kw}
    r = await c.post("/indexers", json=body)
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


async def _movie(c: httpx.AsyncClient) -> dict[str, Any]:
    profiles = (await c.get("/quality-profiles")).json()
    hd = next(p["id"] for p in profiles if p["name"] == "Standard HD")
    r = await c.post("/titles", json={"tmdb_id": 438631, "profile_id": hd})
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


ITEMS = [
    Item("Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX", infohash="a" * 40, seeders=80),
    Item("Dune.2021.720p.BluRay.x264-SPARKS", infohash="b" * 40, size=3 * GB),
    Item("Dune.2021.2160p.WEB-DL.HEVC-GRP", infohash="c" * 40, size=20 * GB),
    Item("Dune.Part.Two.2024.1080p.WEB-DL-GRP", infohash="d" * 40),
    Item("Dune.2021.1080p.WEB-DL.HC-NOPE", infohash="e" * 40, seeders=0),
]
