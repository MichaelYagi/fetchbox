"""Direct (HTTP) downloads and web-seed releases: resume, restart, errors, and the
built-in Internet Archive definition end to end against a fake archive.org."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import httpx
import libtorrent as lt
import pytest

from fetchbox.providers.download_clients import AddOptions, DownloadClientError
from fetchbox.providers.download_clients.direct import DirectClient, DirectSettings
from fetchbox.providers.indexers import IndexerError, TorrentSource
from fetchbox.providers.indexers.definition import webseed_file
from tests.conftest import Running
from tests.pipeline import Fakes
from tests.torrents import until

DATA = bytes(range(256)) * 4096  # 1 MiB


class FileServer:
    """Serves DATA at /f/movie file.mp4 with Range support; can drop the connection once."""

    def __init__(self) -> None:
        self.cut_after: int | None = None
        self.requests: list[str | None] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        if not req.url.path.startswith("/f/"):
            return httpx.Response(404)
        rng = req.headers.get("range")
        self.requests.append(rng)
        start = int(rng.removeprefix("bytes=").rstrip("-")) if rng else 0
        body = DATA[start:]
        headers = {"content-length": str(len(body))}
        status = 206 if rng else 200
        if self.cut_after is not None:
            cut, self.cut_after = self.cut_after, None

            async def broken() -> Any:
                yield body[:cut]
                raise httpx.ReadError("connection reset")

            return httpx.Response(status, headers=headers, stream=_Stream(broken()))
        return httpx.Response(status, headers=headers, content=body)


class _Stream(httpx.AsyncByteStream):
    def __init__(self, gen: Any) -> None:
        self.gen = gen

    async def __aiter__(self) -> Any:
        async for chunk in self.gen:
            yield chunk


URL = "https://files.example/f/movie%20file.mp4"


def _client(server: FileServer, state: Path | None = None) -> DirectClient:
    return DirectClient(
        DirectSettings(), state_dir=state, transport=httpx.MockTransport(server.handle)
    )


async def _done(c: DirectClient, ref: str) -> None:
    async def ok() -> bool:
        [st] = await c.status([ref])
        return st.state in ("seeding", "error")

    await until(ok, within=20)


async def test_download_resumes_after_a_dropped_connection(tmp_path: Path) -> None:
    srv = FileServer()
    srv.cut_after = 300_000
    c = _client(srv)
    await c.start()
    src = TorrentSource(url=URL, name="Item", size=len(DATA))
    ref = await c.add(src, AddOptions(save_path=str(tmp_path), paused=False))
    assert ref == hashlib.sha1(URL.encode()).hexdigest()
    assert await c.add(src, AddOptions(save_path=str(tmp_path))) == ref  # same file once
    await _done(c, ref)
    [st] = await c.status([ref])
    assert st.state == "seeding" and st.progress == 1.0 and st.size == len(DATA)
    out = tmp_path / "Item" / "movie file.mp4"
    assert out.read_bytes() == DATA and not out.with_name("movie file.mp4.part").exists()
    first, again = srv.requests
    assert first is None and again is not None and again != "bytes=0-"  # resumed
    [f] = await c.files(ref)
    assert f.path == "Item/movie file.mp4" and f.progress == 1.0
    await c.remove(ref, True)
    assert not out.exists() and not out.parent.exists()
    await c.stop()


async def test_paused_download_survives_a_restart(tmp_path: Path) -> None:
    srv = FileServer()
    state = tmp_path / "state"
    c = _client(srv, state)
    await c.start()
    ref = await c.add(TorrentSource(url=URL, name="Item"), AddOptions(save_path=str(tmp_path)))
    [st] = await c.status([ref])
    assert st.state == "paused" and srv.requests == []
    await c.stop()

    c2 = _client(srv, state)
    await c2.start()
    [st] = await c2.status([ref])
    assert st.state == "paused"
    await c2.resume(ref)
    await _done(c2, ref)
    assert (tmp_path / "Item" / "movie file.mp4").read_bytes() == DATA
    await c2.stop()


async def test_missing_file_is_an_error(tmp_path: Path) -> None:
    c = _client(FileServer())
    await c.start()
    ref = await c.add(
        TorrentSource(url="https://files.example/nope.mp4"),
        AddOptions(save_path=str(tmp_path), paused=False),
    )
    await _done(c, ref)
    [st] = await c.status([ref])
    assert st.state == "error" and "404" in (st.error or "")
    with pytest.raises(DownloadClientError):
        await c.add(TorrentSource(magnet="magnet:?xt=urn:btih:" + "a" * 40), AddOptions("/x"))
    await c.stop()


def _webseeded(tmp_path: Path, seed: str = "https://archive.example/download/") -> bytes:
    root = tmp_path / "t"
    item = root / "notld_1968"
    item.mkdir(parents=True)
    (item / "Night of the Living Dead.mp4").write_bytes(DATA)
    (item / "notld.ogv").write_bytes(DATA[:1000])
    (item / "notld_meta.xml").write_bytes(b"<x/>")
    fs = lt.file_storage()
    lt.add_files(fs, str(item))
    ct = lt.create_torrent(fs, 16 * 1024)
    ct.add_url_seed(seed)
    lt.set_piece_hashes(ct, str(root))
    return bytes(lt.bencode(ct.generate()))


def test_webseed_picks_the_largest_video(tmp_path: Path) -> None:
    src = webseed_file(_webseeded(tmp_path))
    assert src.url == (
        "https://archive.example/download/notld_1968/Night%20of%20the%20Living%20Dead.mp4"
    )
    assert src.name == "notld_1968" and src.size == len(DATA)
    with pytest.raises(IndexerError, match="no web seed"):
        root = tmp_path / "plain"
        (root / "x").mkdir(parents=True)
        (root / "x" / "a.mkv").write_bytes(b"1" * 100)
        fs = lt.file_storage()
        lt.add_files(fs, str(root / "x"))
        ct = lt.create_torrent(fs, 16 * 1024)
        lt.set_piece_hashes(ct, str(root))
        webseed_file(bytes(lt.bencode(ct.generate())))


async def test_internet_archive_end_to_end(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, tmp_path: Path, settings: Any
) -> None:
    """The real built-in definition against a fake archive.org: search, the item's
    torrent, the movie file from its web seed, and a correctly named file in the
    library. Nothing but Fetchbox involved: the zero-config path."""
    server.app.state.downloads.poll_interval = 0.1
    server.app.state.library.sweep_interval = 0.2
    server.app.state.autosearch.sweep_interval = 0.2
    torrent = _webseeded(tmp_path, seed="https://archive.org/download/")

    def archive(req: httpx.Request) -> httpx.Response:
        assert req.url.host.endswith("archive.org"), req.url
        if req.url.path == "/advancedsearch.php":
            docs = [{"identifier": "notld_1968", "title": "Night of the Living Dead",
                     "year": "1968", "item_size": 2 * len(DATA)}]  # fmt: skip
            return httpx.Response(200, json={"response": {"docs": docs}})
        if req.url.path == "/download/notld_1968/notld_1968_archive.torrent":
            return httpx.Response(200, content=torrent)
        if req.url.path == "/download/notld_1968/Night of the Living Dead.mp4":
            return httpx.Response(200, content=DATA)
        return httpx.Response(404)

    mock = httpx.MockTransport(archive)
    server.app.state.sources.provider_kwargs = {"transport": mock}
    server.app.state.downloads.provider_kwargs = {"transport": mock}
    clients = {x["kind"]: x for x in (await c.get("/download-clients")).json()}
    r = await c.patch(f"/download-clients/{clients['direct']['id']}", json={"settings": {}})
    assert r.status_code == 200  # restarted with the fake transport

    fakes.tmdb.movies[10331] = {
        **fakes.tmdb.movies[438631],
        "id": 10331, "title": "Night of the Living Dead",
        "original_title": "Night of the Living Dead", "release_date": "1968-10-01",
        "imdb_id": None, "runtime": 96,
    }  # fmt: skip
    body = {"kind": "definition", "name": "Internet Archive",
            "settings": {"definition": "internet-archive"}}  # fmt: skip
    assert (await c.post("/indexers", json=body)).status_code == 201
    anyp = next(p["id"] for p in (await c.get("/quality-profiles")).json() if p["name"] == "Any")
    movie = (await c.post("/titles", json={"tmdb_id": 10331, "profile_id": anyp})).json()

    async def acquired() -> bool:
        w = (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]
        return bool(w["state"] == "acquired")

    await until(acquired, within=30)
    [f] = (await c.get(f"/titles/{movie['id']}/files")).json()
    expected = "Night of the Living Dead (1968) {tmdb-10331}/Night of the Living Dead (1968).mp4"
    assert f["relative_path"] == expected, f["relative_path"]
    placed = settings.downloads_dir / "Movies" / expected
    assert placed.read_bytes() == DATA

    async def dropped() -> bool:  # the direct copy goes once imported (nothing to seed)
        [d] = (await c.get("/downloads")).json()
        return bool(d["seeded_at"])

    await until(dropped, within=10)
    await asyncio.sleep(0.2)
    assert placed.read_bytes() == DATA  # the one file, filed
    leftovers = settings.downloads_dir / "Movies" / ".incomplete"
    assert not list(leftovers.rglob("*.mp4"))  # moved into place, nothing left behind
