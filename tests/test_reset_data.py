"""Settings → Delete all data: the data goes, the settings stay, a backup is made first;
media files stay unless asked."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from fetchbox.providers.download_clients.builtin import BuiltinClient
from tests.conftest import Running
from tests.fake_torznab import Item
from tests.pipeline import Fakes, _indexer, _movie
from tests.test_library_api import EXPECTED, _acquire, _engine, fast, made, seed  # noqa: F401
from tests.torrents import Made, make_torrent, until

MB = 1024 * 1024
CONFIRM = {"confirm": "delete all data"}


async def _settings(c: httpx.AsyncClient) -> dict[str, Any]:
    async def ids(path: str) -> list[str]:
        return sorted(x["id"] for x in (await c.get(path)).json())

    return {
        "indexers": await ids("/indexers"),
        "clients": await ids("/download-clients"),
        "folders": await ids("/destinations"),
        "profiles": await ids("/quality-profiles"),
        "formats": await ids("/custom-formats"),
        "config": (await c.get("/system/config")).json()["config"],
    }


async def _activity(c: httpx.AsyncClient) -> list[str]:
    items = (await c.get("/activity", params={"limit": 100})).json()["items"]
    return [a["message"] for a in items]


@pytest.mark.parametrize("delete_files", [False, True])
async def test_delete_all_data_keeps_the_settings(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,  # noqa: F811
    seed: BuiltinClient,  # noqa: F811
    settings: Any,
    delete_files: bool,
) -> None:
    await _acquire(c, fakes, server, made, seed)
    library = settings.downloads_dir / "Movies"
    placed = library / EXPECTED
    assert placed.is_file()
    before = await _settings(c)
    backups = len((await c.get("/backups")).json())

    r = await c.post("/system/reset-data", json={"confirm": "yes"})
    assert r.status_code == 422  # the guard phrase is required
    assert (await c.get("/titles")).json()["items"]  # nothing happened
    r = await c.post("/system/reset-data", json={**CONFIRM, "delete_files": delete_files})
    assert r.status_code == 200, r.text
    out = r.json()

    # the data is gone
    assert (await c.get("/titles")).json()["items"] == []
    assert (await c.get("/downloads")).json() == []
    assert (await c.get("/blocklist")).json() == []
    assert (await c.get("/imports")).json() == []
    # the settings are not
    assert await _settings(c) == before
    # a backup came first: restoring it undoes the reset
    listed = (await c.get("/backups")).json()
    assert len(listed) == backups + 1 and out["backup"] in {b["id"] for b in listed}

    if delete_files:  # into the folder's recycle folder, and the movie's folder goes
        assert out["files_deleted"] == 1 and not placed.exists()
        assert not placed.parent.exists()
        assert list((library / ".fetchbox-recycle").rglob("*.mkv"))
    else:  # the file stays, ready to import again
        assert out["files_deleted"] == 0 and placed.is_file()

    # the log starts afresh, with the reset itself
    async def logged() -> bool:
        return any(m.startswith("All data deleted") for m in await _activity(c))

    await until(logged, within=10)
    messages = await _activity(c)  # newest first
    # nothing about the library from before the reset is left (the title added, its
    # download, its import). System entries can follow it: a health check emitted just
    # before the reset is written in the background, and may land just after it (seen
    # on CI: "Destination TV is online" after "All data deleted").
    assert sum(m.startswith("All data deleted") for m in messages) == 1, messages
    assert not [m for m in messages if m.startswith(("Added ", "Imported", "Grabbed"))], messages

    # and Fetchbox carries on: the same movie can be added again
    again = await _movie(c)
    assert again["wanted"][0]["state"] == "missing"


async def test_delete_all_data_stops_unfinished_downloads(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, settings: Any, tmp_path: Path
) -> None:
    """A download in progress is stopped and its partial data deleted (no seeder here,
    so it never finishes)."""
    t = make_torrent(tmp_path / "t", "Dune.2021.1080p.WEB-DL-X", {"x.mkv": 2 * MB})
    await _indexer(c, fakes, "one", [Item(t.name, magnet=t.magnet, infohash=t.info_hash)])
    movie = await _movie(c)
    wid = movie["wanted"][0]["id"]
    rel = (await c.get("/releases", params={"wanted_item_id": wid})).json()["releases"][0]
    r = await c.post("/grabs", json={"release_id": rel["release_id"], "allow_rejected": True})
    assert r.status_code == 201, r.text

    async def downloading() -> bool:
        return any(d["status"] == "downloading" for d in (await c.get("/downloads")).json())

    await until(downloading, within=20)
    assert await _engine(server).status()

    r = await c.post("/system/reset-data", json=CONFIRM)
    assert r.status_code == 200, r.text
    assert r.json()["downloads_stopped"] == 1 and r.json()["not_stopped"] == []
    assert await _engine(server).status() == []  # the engine has nothing left
    assert (await c.get("/downloads")).json() == []
    assert not (settings.downloads_dir / "Movies" / ".incomplete" / t.name).exists()
