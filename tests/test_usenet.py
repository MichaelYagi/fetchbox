"""Usenet: Newznab indexers, SABnzbd and NZBGet, and a release from search to library.

The clients' responses are shaped on ones recorded from a real SABnzbd 5.1.3 and NZBGet
26.3 (tests/fixtures/usenet), which downloaded, verified and filed real NZBs from a local
news server; the fakes write finished jobs to disk where each client does."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fetchbox.providers.download_clients import AddOptions, DownloadClientError
from fetchbox.providers.download_clients.usenet import (
    NzbgetClient,
    NzbgetSettings,
    SabnzbdClient,
    SabnzbdSettings,
)
from fetchbox.providers.indexers import IndexerError, SearchQuery, TorrentSource
from fetchbox.providers.indexers.torznab import NewznabProvider, NewznabSettings
from tests.conftest import Running
from tests.fake_torznab import API_KEY, NZB, FakeTorznab, Item
from tests.fake_usenet import NZBGET_AUTH, SAB_KEY, FakeNzbget, FakeSab, UsenetStore
from tests.pipeline import Fakes, _indexer, _movie
from tests.torrents import until

FIX = Path(__file__).parent / "fixtures" / "usenet"
NAME = "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"
VIDEO = b"\x1a\x45\xdf\xa3" + b"\0" * 400_000


def _recorded(name: str) -> Any:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8").replace("{ROOT}", "/dl"))


def _sab(handler: Any, key: str = SAB_KEY) -> SabnzbdClient:
    return SabnzbdClient(
        SabnzbdSettings(url="http://sab:8080", api_key=key),
        transport=httpx.MockTransport(handler),
    )


def _nzbget(handler: Any, password: str = NZBGET_AUTH[1]) -> NzbgetClient:
    return NzbgetClient(
        NzbgetSettings(url="http://nzbget:6789", username="nzbget", password=password),
        transport=httpx.MockTransport(handler),
    )


# ---- the clients' real responses ---------------------------------------------------------------


async def test_sabnzbd_reads_its_real_responses() -> None:
    """Recorded from SABnzbd 5.1.3: a paused job in the queue, a finished one (a single
    file, which SABnzbd reports as the file's path) and a failed one."""
    queue, history = _recorded("sab-queue"), _recorded("sab-history")

    def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=queue if req.url.params["mode"] == "queue" else history)

    jobs = {t.name: t for t in await _sab(handle).status()}
    waiting = jobs["Waiting.Show.S01E01.1080p.WEB-DL-GRP"]
    assert (waiting.state, waiting.progress, waiting.download_rate) == ("paused", 0.0, 0)
    done = jobs["Some.Movie.2026.1080p.WEB-DL-GRP"]
    assert (done.state, done.progress) == ("seeding", 1.0)  # complete (nothing to share)
    assert done.save_path == "/dl/complete/Some.Movie.2026.1080p.WEB-DL-GRP"  # its folder
    gone = jobs["Gone.Movie.2024.720p.WEB-DL-GRP"]
    assert gone.state == "error" and gone.error == "Download failed - Not on your server(s)"
    assert done.ref == history["history"]["slots"][1]["nzo_id"]  # its id, queue to history


async def test_nzbget_reads_its_real_responses() -> None:
    """Recorded from NZBGet 26.3: a paused job, a finished one, a failed one."""
    answers = {"listgroups": "nzbget-listgroups", "history": "nzbget-history",
               "status": "nzbget-status"}  # fmt: skip

    def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_recorded(answers[json.loads(req.content)["method"]]))

    jobs = {t.name: t for t in await _nzbget(handle).status()}
    assert jobs["wait"].state == "paused" and jobs["wait"].ref == "3"
    done = jobs["Other.Movie.2025.1080p.BluRay-GRP"]
    assert (done.state, done.progress) == ("seeding", 1.0)
    assert done.save_path == "/dl/completed/Movies/Other.Movie.2025.1080p.BluRay-GRP"
    assert jobs["gone"].state == "error"
    assert jobs["gone"].error == "NZBGet: articles missing from your news server(s)"


# ---- each client through a job's life ---------------------------------------------------------


@pytest.mark.parametrize("kind", ["sabnzbd", "nzbget"])
async def test_lifecycle(kind: str, tmp_path: Path) -> None:
    store = UsenetStore(local=tmp_path, remote="/downloads")
    client = _sab(FakeSab(store).handle) if kind == "sabnzbd" else _nzbget(FakeNzbget(store).handle)
    assert (await client.test()).ok
    with pytest.raises(DownloadClientError, match="NZB files only"):
        await client.add(TorrentSource(magnet="magnet:?xt=urn:btih:" + "a" * 40), AddOptions(None))

    ref = await client.add(TorrentSource(nzb=NZB, name=NAME), AddOptions(None, category="movies"))
    [t] = await client.status([ref])
    assert (t.ref, t.name, t.state) == (ref, NAME, "paused")  # added paused, named
    assert store.jobs[ref].nzb == NZB and store.jobs[ref].category == "movies"
    await client.resume(ref)
    [t] = await client.status([ref])
    assert t.state == "downloading" and t.download_rate > 0 and t.seeds == 0
    await client.pause(ref)
    assert (await client.status([ref]))[0].state == "paused"

    under = "complete" if kind == "sabnzbd" else "completed/movies"
    folder = store.finish(ref, {f"{NAME}.mkv": VIDEO, "sample/sample.mkv": b"x"}, under)
    [t] = await client.status([ref])
    assert t.state == "seeding" and t.save_path == store.client_path(folder)
    with pytest.raises(DownloadClientError):
        await client.files(ref)  # a finished job's files are listed from its folder

    await client.remove(ref, delete_data=True)
    assert await client.status([ref]) == []
    # SABnzbd deletes the job's files; NZBGet keeps them (Fetchbox deletes that folder)
    assert folder.exists() == (kind == "nzbget")
    await client.stop()


async def test_wrong_keys_say_where_to_find_them() -> None:
    store = UsenetStore(local=Path("/nowhere"), remote="/downloads")
    sab = await _sab(FakeSab(store).handle, key="0" * 32).test()
    assert not sab.ok and "Config > General > Security > API Key" in sab.message
    nzbget = await _nzbget(FakeNzbget(store).handle, password="nope").test()
    assert not nzbget.ok and "ControlUsername" in nzbget.message

    def not_it(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>a router</html>")

    r = await _sab(not_it).test()
    assert not r.ok and "no SABnzbd at http://sab:8080" in r.message


# ---- Newznab ---------------------------------------------------------------------------------


async def test_newznab_lists_nzbs_and_fetches_them() -> None:
    fake = FakeTorznab([Item(NAME, nzb=True, seeders=None, peers=None, extra={"grabs": "42"})])
    p = NewznabProvider(
        NewznabSettings(url="http://nzbindexer", api_key=API_KEY), transport=fake.transport
    )
    [c] = await p.search(SearchQuery("movie", ("Dune",), year=2021))
    assert (c.protocol, c.info_hash, c.magnet_url, c.grabs, c.seeders) == (
        "usenet", None, None, 42, None)  # fmt: skip
    src = await p.resolve(c)
    assert src.protocol == "usenet" and src.nzb == NZB and src.name == NAME

    fake_bad = FakeTorznab([Item(NAME, nzb=False)])  # a link that serves a .torrent instead
    bad = NewznabProvider(
        NewznabSettings(url="http://nzbindexer", api_key=API_KEY), transport=fake_bad.transport
    )
    [c] = await bad.search(SearchQuery("movie", ("Dune",), year=2021))
    assert c.protocol == "usenet"  # a Newznab feed lists NZBs
    with pytest.raises(IndexerError, match="not an NZB"):
        await bad.resolve(c)
    await p.aclose()
    await bad.aclose()


def test_newznab_attributes_are_read() -> None:
    """Usenet indexers tag attributes newznab:attr, not torznab:attr: sizes, categories and
    grabs come from them."""
    import xml.etree.ElementTree as ET

    from fetchbox.providers.indexers.torznab import parse_item

    item = ET.fromstring(
        '<item xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
        f"<title>{NAME}</title><guid>abc</guid><link>http://i/getnzb/1</link>"
        '<enclosure url="http://i/getnzb/1" length="0" type="application/x-nzb"/>'
        '<newznab:attr name="category" value="2040"/><newznab:attr name="size" value="4000"/>'
        '<newznab:attr name="grabs" value="57"/></item>'
    )
    c = parse_item(item)
    assert c is not None and (c.protocol, c.size, c.categories, c.grabs) == (
        "usenet", 4000, (2040,), 57)  # fmt: skip


# ---- from search to library --------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["sabnzbd", "nzbget"])
async def test_a_usenet_release_from_search_to_library(
    kind: str, c: httpx.AsyncClient, fakes: Fakes, server: Running, settings: Any, tmp_path: Path
) -> None:
    """Found on a Newznab indexer, its NZB handed to the client paused, resumed by
    Fetchbox, filed from the finished job's folder (through a path mapping, as with a
    NAS), then the job is removed from the client with its files."""
    server.app.state.downloads.poll_interval = 0.1
    server.app.state.library.sweep_interval = 0.2
    local = tmp_path / "usenet"
    store = UsenetStore(local=local, remote="/downloads")
    fake: Any = FakeSab(store) if kind == "sabnzbd" else FakeNzbget(store)
    server.app.state.downloads.provider_kwargs = {"transport": httpx.MockTransport(fake.handle)}

    settings_ = (
        {"url": "http://sab:8080", "api_key": SAB_KEY}
        if kind == "sabnzbd"
        else {"url": "http://nzbget:6789", "username": "nzbget", "password": NZBGET_AUTH[1]}
    )
    r = await c.post("/download-clients", json={
        "kind": kind, "name": kind, "settings": settings_,
        "policy": {"path_mappings": [{"local": str(local), "client": "/downloads"}]},
    })  # fmt: skip
    assert r.status_code == 201, r.text
    assert (await c.post(f"/download-clients/{r.json()['id']}/test")).json()["ok"]

    nzb = Item(NAME, nzb=True, seeders=None, peers=None, link=f"http://nzbs/dl/0?apikey={API_KEY}")
    await _indexer(c, fakes, "nzbs", [nzb], kind="newznab")
    movie = await _movie(c, search=True)

    async def in_client() -> bool:
        return bool(store.jobs)

    await until(in_client, within=20)
    [job] = store.jobs.values()
    assert job.name == NAME and job.nzb == NZB

    async def resumed() -> bool:
        return not store.jobs[job.ref].paused

    await until(resumed, within=10)  # Fetchbox's queue manager started it
    under = "complete" if kind == "sabnzbd" else "completed/movies"
    folder = store.finish(job.ref, {f"{NAME}.mkv": VIDEO, "Sample/sample.mkv": b"x" * 10}, under)

    async def acquired() -> bool:
        w = (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]
        return bool(w["state"] == "acquired")

    await until(acquired, within=20)
    [f] = (await c.get(f"/titles/{movie['id']}/files")).json()
    assert (settings.downloads_dir / "Movies" / f["relative_path"]).read_bytes() == VIDEO

    async def cleaned_up() -> bool:  # nothing to seed: removed once filed, files and all
        return not store.jobs and not folder.exists()

    await until(cleaned_up, within=15)
    [d] = (await c.get("/downloads")).json()
    assert d["status"] == "completed" and d["seeded_at"]
