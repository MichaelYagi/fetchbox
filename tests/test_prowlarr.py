"""The Prowlarr indexer kind: one address and key, every Prowlarr indexer searched.

The fake answers like a real Prowlarr 3.0 (checked against one): /api/v1/indexer
lists indexers as JSON behind X-Api-Key, each indexer has a Torznab feed at /<id>/api.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from fetchbox.providers.indexers import IndexerError, SearchQuery
from fetchbox.providers.indexers.prowlarr import ProwlarrProvider, ProwlarrSettings
from tests.conftest import Running
from tests.fake_torznab import FakeTorznab, Item
from tests.pipeline import Fakes, _movie

KEY = "0123456789abcdef0123456789abcdef"


class FakeProwlarr:
    def __init__(self) -> None:
        self.indexers: list[dict[str, Any]] = [
            {"id": 1, "name": "Public One", "enable": True, "protocol": "torrent",
             "supportsSearch": True},
            {"id": 2, "name": "Private Two", "enable": True, "protocol": "torrent",
             "supportsSearch": True},
            {"id": 3, "name": "Disabled", "enable": False, "protocol": "torrent"},
            {"id": 4, "name": "Usenet", "enable": True, "protocol": "usenet"},
        ]  # fmt: skip
        self.feeds = {
            1: FakeTorznab([Item("Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX",
                                 infohash="a" * 40)], key=KEY),
            2: FakeTorznab([Item("Dune.2021.2160p.WEB-DL.HEVC-GRP", infohash="c" * 40)],
                           key=KEY),
            4: FakeTorznab([Item("Dune.2021.1080p.BluRay.x264-NZBGRP", nzb=True, seeders=None,
                                 peers=None, extra={"grabs": "120"},
                                 link=f"http://prowlarr:9696/4/dl/nzb?apikey={KEY}")], key=KEY),
        }  # fmt: skip
        self.down: set[int] = set()

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.startswith("/api/v1/"):
            if req.headers.get("x-api-key") != KEY:
                return httpx.Response(401)
            if path == "/api/v1/indexer":
                return httpx.Response(200, json=self.indexers)
            return httpx.Response(404, json={})
        if path == "/api":  # Prowlarr's own API root, not a feed
            return httpx.Response(406)
        head = path.strip("/").split("/")[0]
        if head.isdigit() and int(head) in self.feeds:
            if int(head) in self.down:
                return httpx.Response(500)
            if req.url.params.get("apikey") != KEY:
                return httpx.Response(401)
            return self.feeds[int(head)].handle(req)  # it reads the query, not the path
        return httpx.Response(404, text="<html>not found</html>")


def _provider(
    fake: FakeProwlarr, url: str = "http://prowlarr:9696", key: str = KEY
) -> ProwlarrProvider:
    return ProwlarrProvider(
        ProwlarrSettings(url=url, api_key=key), transport=httpx.MockTransport(fake.handle)
    )


def test_settings_take_the_base_address() -> None:
    for pasted in ("http://p:9696", "http://p:9696/", "http://p:9696/3", "http://p:9696/3/api/"):
        assert ProwlarrSettings(url=pasted, api_key=KEY).url == "http://p:9696"
    assert ProwlarrSettings(url="http://p/prowlarr", api_key=f" {KEY}\n").url == "http://p/prowlarr"
    s = ProwlarrSettings(url="http://p:9696", api_key=f" {KEY}\n")
    assert s.api_key.get_secret_value() == KEY
    with pytest.raises(ValueError):
        ProwlarrSettings(url="p:9696", api_key=KEY)


async def test_searches_every_enabled_indexer() -> None:
    """Torrent and Usenet indexers alike; a Usenet one's results are NZBs."""
    fake = FakeProwlarr()
    p = _provider(fake)
    result = await p.test()
    assert result.ok and result.details["indexers"] == ["Private Two", "Public One", "Usenet"]
    found = await p.search(SearchQuery("movie", ("Dune",), year=2021))
    assert sorted(c.title for c in found) == [
        "Dune.2021.1080p.BluRay.x264-NZBGRP", "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX",
        "Dune.2021.2160p.WEB-DL.HEVC-GRP",
    ]  # fmt: skip
    nzb = next(c for c in found if c.title.endswith("NZBGRP"))
    assert (nzb.protocol, nzb.info_hash, nzb.magnet_url, nzb.grabs) == ("usenet", None, None, 120)
    assert {c.protocol for c in found if c is not nzb} == {"torrent"}
    src = await p.resolve(nzb)  # the NZB itself, fetched through Prowlarr
    assert src.protocol == "usenet" and src.nzb and b"<nzb" in src.nzb and src.name == nzb.title

    # one indexer down: the others still answer; all down: the search fails
    fake.down = {2, 4}
    assert [c.title for c in await p.search(SearchQuery("movie", ("Dune",)))] == [
        "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"
    ]
    fake.down = {1, 2, 4}
    with pytest.raises(IndexerError, match="every Prowlarr indexer failed"):
        await p.search(SearchQuery("movie", ("Dune",)))

    # an indexer added in Prowlarr is picked up on the next listing
    fake.down = set()
    fake.indexers.append({"id": 5, "name": "New", "enable": True, "protocol": "torrent"})
    fake.feeds[5] = FakeTorznab([], key=KEY)
    assert len(await p.indexers(fresh=True)) == 4
    await p.aclose()


async def test_one_slow_indexer_doesnt_lose_the_others_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seen with a real Prowlarr: LimeTorrents took 30 s and timed out, and every other
    indexer's results (The Pirate Bay: 71 in 0.3 s) were lost with it. Each indexer now
    has its own deadline, inside the source's, and a slow one is left out alone."""
    from fetchbox.providers.indexers import prowlarr

    monkeypatch.setattr(prowlarr, "CHILD_DEADLINE", 0.5)
    fake = FakeProwlarr()

    async def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path.strip("/").split("/")[0] == "2":
            await asyncio.sleep(30)  # a tracker that doesn't answer
        return fake.handle(req)

    p = ProwlarrProvider(ProwlarrSettings(url="http://prowlarr:9696", api_key=KEY),
                         transport=httpx.MockTransport(handle))  # fmt: skip
    found = await asyncio.wait_for(p.search(SearchQuery("movie", ("Dune",))), 2)
    assert sorted(c.title for c in found) == [
        "Dune.2021.1080p.BluRay.x264-NZBGRP", "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"
    ]  # fmt: skip
    await p.aclose()


async def test_searches_right_after_the_machine_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The monotonic clock counts from boot. The indexer list used to start "listed at 0",
    which for the first 10 minutes after boot read as listed just now, with nothing in it:
    every search failed with "Prowlarr has no enabled torrent indexers" (seen on CI)."""
    from fetchbox.providers.indexers import prowlarr

    monkeypatch.setattr(prowlarr.time, "monotonic", lambda: 5.0)  # 5 s after boot
    p = _provider(FakeProwlarr())
    found = await p.search(SearchQuery("movie", ("Dune",), year=2021))
    assert len(found) == 3  # (two torrent indexers and the Usenet one)
    await p.aclose()


async def test_clear_errors() -> None:
    fake = FakeProwlarr()
    bad = _provider(fake, key="wrong")
    r = await bad.test()
    assert not r.ok and "Settings > General > Security > API Key" in r.message
    await bad.aclose()

    not_prowlarr = ProwlarrProvider(
        ProwlarrSettings(url="http://jackett:9117", api_key=KEY),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html></html>")),
    )
    r = await not_prowlarr.test()
    assert not r.ok and "no Prowlarr at" in r.message
    await not_prowlarr.aclose()

    fake.indexers = []
    empty = _provider(fake)
    r = await empty.test()
    assert not r.ok and "no enabled indexers" in r.message
    await empty.aclose()


async def test_add_by_address_and_search(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    fake = FakeProwlarr()
    server.app.state.sources.provider_kwargs = {"transport": httpx.MockTransport(fake.handle)}
    kinds = [k["kind"] for k in (await c.get("/indexers/kinds")).json()]
    assert "prowlarr" in kinds
    r = await c.post("/indexers", json={"kind": "prowlarr", "name": "Prowlarr",
                                        "settings": {"url": "http://prowlarr:9696/3/api",
                                                     "api_key": KEY}})  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["settings"]["url"] == "http://prowlarr:9696"
    assert r.json()["settings"]["api_key"] == "********"
    t = (await c.post(f"/indexers/{r.json()['id']}/test")).json()
    assert t["ok"] and "3 indexer(s)" in t["message"]

    movie = await _movie(c)
    res = (await c.get("/releases", params={"wanted_item_id": movie["wanted"][0]["id"]})).json()
    titles = {x["candidate"]["title"]: x for x in res["releases"]}
    assert titles["Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"]["accepted"]
    assert all(x["candidate"]["source_name"] == "Prowlarr" for x in res["releases"])
    # the Usenet indexer's NZB: found, but there's no Usenet client to download it
    nzb = titles["Dune.2021.1080p.BluRay.x264-NZBGRP"]
    assert nzb["candidate"]["protocol"] == "usenet" and not nzb["accepted"]
    assert "no_client" in [r["code"] for r in nzb["rejections"]]
