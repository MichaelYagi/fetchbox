"""A title's own library folder, chosen when adding it (the Add dialog's Folder field)."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import httpx

from fetchbox.providers.download_clients import DownloadClientError
from fetchbox.providers.download_clients.builtin import BuiltinClient
from tests.conftest import Running
from tests.fake_torznab import Item
from tests.pipeline import Fakes, _indexer, standard_hd
from tests.test_library_api import EXPECTED, NAME, _engine, fast, made, seed  # noqa: F401
from tests.test_musicbrainz import ARTIST, fake_mb
from tests.torrents import Made, until

DUNE = 438631


async def _folders(c: httpx.AsyncClient, kind: str) -> list[dict[str, Any]]:
    return [d for d in (await c.get("/destinations")).json() if d["media_kind"] == kind]


async def _add(c: httpx.AsyncClient, folder: str | None, **kw: Any) -> httpx.Response:
    body = {"tmdb_id": DUNE, "profile_id": await standard_hd(c), "search": False, **kw}
    if folder is not None:
        body["folder"] = folder
    return await c.post("/titles", json=body)


async def test_a_movie_downloads_into_its_own_folder(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,  # noqa: F811
    seed: BuiltinClient,  # noqa: F811
    settings: Any,
    tmp_path: Path,
) -> None:
    own = tmp_path / "Kids" / "Movies"  # doesn't exist yet: it's made
    await _indexer(c, fakes, "one", [Item(NAME, magnet=made.magnet, infohash=made.info_hash)])
    r = await _add(c, str(own), search=True)
    assert r.status_code == 201, r.text
    movie = r.json()
    [default, extra] = await _folders(c, "movie")
    assert default["is_default"] and extra["path"] == str(own) and extra["name"] == "Movies 2"
    assert movie["destination_id"] == extra["id"]
    assert extra["naming_template_id"] == default["naming_template_id"]  # named the same way

    async def acquired() -> bool:
        with contextlib.suppress(DownloadClientError):  # gone once filed (no seeding)
            _engine(server).connect_peer(made.info_hash, "127.0.0.1", seed.listen_port or 0)
        t = (await c.get(f"/titles/{movie['id']}")).json()
        return bool(t["wanted"][0]["state"] == "acquired")

    await until(acquired, within=60)
    assert (own / EXPECTED).is_file()  # downloaded and filed there
    assert not (settings.downloads_dir / "Movies" / EXPECTED).exists()  # not the default


async def test_the_default_folder_or_an_existing_one_is_not_added_again(
    c: httpx.AsyncClient, settings: Any
) -> None:
    default = (await _folders(c, "movie"))[0]
    # the default folder, typed out: the title follows Settings (no folder of its own)
    r = await _add(c, default["path"] + "/")
    assert r.status_code == 201, r.text
    assert r.json()["destination_id"] is None
    assert len(await _folders(c, "movie")) == 1


async def test_a_folder_that_cant_be_used_adds_nothing(
    c: httpx.AsyncClient, tmp_path: Path
) -> None:
    # (not "~someone/Movies": on Windows that expands to C:\\Users\\someone\\Movies, a full path)
    for bad in ("Movies/Kids", "Kids"):
        r = await _add(c, bad)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["errors"][0]["field"] == "body.folder"
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    r = await _add(c, str(blocker / "Movies"))  # its parent is a file
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "body.folder"
    assert (await c.get("/titles")).json()["items"] == []  # nothing added
    assert len(await _folders(c, "movie")) == 1  # and no folder either

    # a title that's already there: refused before any folder is made
    assert (await _add(c, None)).status_code == 201
    r = await _add(c, str(tmp_path / "Elsewhere"))
    assert r.status_code == 409
    assert len(await _folders(c, "movie")) == 1 and not (tmp_path / "Elsewhere").exists()


async def test_an_artist_gets_its_own_music_folder(
    c: httpx.AsyncClient, server: Running, tmp_path: Path
) -> None:
    music = server.app.state.music
    music.transport, music.interval = fake_mb(), 0
    lossless = next(p["id"] for p in (await c.get("/quality-profiles")).json()
                    if p["name"] == "Lossless")  # fmt: skip
    own = tmp_path / "Vinyl rips"
    r = await c.post("/music/artists", json={"mbid": ARTIST, "profile_id": lossless,
                                             "folder": str(own)})  # fmt: skip
    assert r.status_code == 201, r.text
    extra = next(d for d in await _folders(c, "music") if d["path"] == str(own))
    assert r.json()["destination_id"] == extra["id"] and extra["name"] == "Vinyl rips"
    assert own.is_dir()
