"""Library folders stay one per real folder and never lose track of what's in them: one
folder written two ways (a symlink; on a Mac /Volumes/Macintosh HD/X is /X) is one
library folder, old duplicates are merged, a folder that still holds titles can't be
removed, and moving one to a new folder keeps the old folder's files where they are."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from fetchbox.core.events import Actor
from fetchbox.modules.library.models import MediaFile, MediaFileItem
from fetchbox.modules.media.models import Title, WantedItem
from fetchbox.modules.storage.models import Destination
from fetchbox.providers.media_servers import Library, covering, place, same_path
from tests.conftest import Running
from tests.pipeline import standard_hd

symlinks = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin on Windows")


def _alias(tmp_path: Path) -> tuple[Path, Path]:
    real = tmp_path / "Media" / "Movies"
    real.mkdir(parents=True)
    os.symlink(tmp_path / "Media", tmp_path / "Disk")
    return real, tmp_path / "Disk" / "Movies"


async def _add(c: httpx.AsyncClient, name: str, path: Path) -> dict[str, Any]:
    r = await c.post("/destinations", json={"name": name, "path": str(path),
                                            "media_kind": "movie"})  # fmt: skip
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


async def _movie_in(
    server: Running, c: httpx.AsyncClient, dest_id: str, *, own: bool = True
) -> str:
    """A title with one file in this library folder, linked as an import links them
    (``own``: the title has this folder as its own, not just the default)."""
    profile = await standard_hd(c)
    node = (await c.get("/destinations")).json()[0]["node_id"]
    async with server.app.state.storage.db.sessionmaker() as s:
        title = Title(kind="movie", tmdb_id=438631, name="Dune", sort_name="dune", year=2021,
                      profile_id=profile, destination_id=dest_id if own else None)  # fmt: skip
        s.add(title)
        await s.flush()
        wanted = WantedItem(title_id=title.id, state="acquired")
        f = MediaFile(storage_node_id=node, destination_id=dest_id, quality={},
                      relative_path="Dune (2021)/Dune.mkv", size=1)  # fmt: skip
        s.add_all([wanted, f])
        await s.flush()
        s.add(MediaFileItem(media_file_id=f.id, wanted_item_id=wanted.id))
        await s.commit()
        return title.id


async def _where(server: Running, title_id: str) -> tuple[str | None, list[str | None]]:
    async with server.app.state.storage.db.sessionmaker() as s:
        t = await s.get(Title, title_id)
        files = list((await s.execute(select(MediaFile.destination_id))).scalars())
    assert t is not None
    return t.destination_id, files


async def _names(c: httpx.AsyncClient) -> list[str]:
    return [d["name"] for d in (await c.get("/destinations")).json()]


@symlinks
async def test_the_same_folder_written_another_way_is_reused(
    client: httpx.AsyncClient, server: Running, tmp_path: Path
) -> None:
    c = client
    real, alias = _alias(tmp_path)
    films = await _add(c, "Films", real)
    storage = server.app.state.storage
    got = await storage.folder_for("movie", str(alias), field="body.folder", actor=Actor())
    assert got == films["id"]
    assert (await _names(c)).count("Films") == 1 and "Movies 2" not in await _names(c)
    # nor can it be added again by hand
    r = await c.post("/destinations", json={"name": "Films again", "path": str(alias),
                                            "media_kind": "movie"})  # fmt: skip
    assert r.status_code == 422 and "already the library folder Films" in r.text


@symlinks
async def test_duplicate_library_folders_are_merged(
    client: httpx.AsyncClient, server: Running, tmp_path: Path
) -> None:
    c = client
    real, alias = _alias(tmp_path)
    keep = (await _add(c, "Films", real))["id"]
    async with server.app.state.storage.db.sessionmaker() as s:  # as an older Fetchbox made it
        node = (await c.get("/destinations")).json()[0]["node_id"]
        dup = Destination(node_id=node, name="Films 2", path=str(alias), media_kind="movie")
        s.add(dup)
        await s.commit()
        dup_id = dup.id
    tid = await _movie_in(server, c, dup_id)

    assert await server.app.state.storage.merge_duplicates() == ["Films 2"]
    assert "Films" in await _names(c) and "Films 2" not in await _names(c)
    assert await _where(server, tid) == (keep, [keep])
    assert await server.app.state.storage.merge_duplicates() == []


async def test_a_folder_holding_titles_cant_be_removed(
    client: httpx.AsyncClient, server: Running, tmp_path: Path
) -> None:
    c = client
    films = await _add(c, "Films", tmp_path / "Films")
    tid = await _movie_in(server, c, films["id"])
    r = await c.delete(f"/destinations/{films['id']}")
    assert r.status_code == 409 and "Films still holds 1 title (1 file)" in r.text
    assert await _where(server, tid) == (films["id"], [films["id"]])  # untouched

    empty = await _add(c, "Empty", tmp_path / "Empty")
    assert (await c.delete(f"/destinations/{empty['id']}")).status_code == 204


async def test_moving_a_library_folder_keeps_whats_in_the_old_one(
    client: httpx.AsyncClient, server: Running, tmp_path: Path
) -> None:
    c = client
    old, new = tmp_path / "Old" / "Movies", tmp_path / "New" / "Movies"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    movies = next(d for d in (await c.get("/destinations")).json() if d["media_kind"] == "movie")
    r = await c.patch(f"/destinations/{movies['id']}", json={"path": str(old)})
    assert r.status_code == 200, r.text  # nothing in it yet: just moves
    assert (
        len([d for d in (await c.get("/destinations")).json() if d["media_kind"] == "movie"]) == 1
    )
    tid = await _movie_in(server, c, movies["id"], own=False)  # in the default folder

    r = await c.patch(f"/destinations/{movies['id']}", json={"path": str(new)})
    assert r.status_code == 200, r.text
    dests = {d["name"]: d for d in (await c.get("/destinations")).json()}
    kept = dests["Movies (old folder)"]
    assert kept["path"] == str(old) and not kept["is_default"]
    assert dests["Movies"]["path"] == str(new) and dests["Movies"]["is_default"]
    assert await _where(server, tid) == (kept["id"], [kept["id"]])  # Dune stays put

    # and back again: the two are one folder once more
    r = await c.patch(f"/destinations/{movies['id']}", json={"path": str(old)})
    assert r.status_code == 200, r.text
    assert "Movies (old folder)" not in await _names(c)
    assert await _where(server, tid) == (None, [movies["id"]])  # follows the default again


def test_server_folders_written_another_way() -> None:
    mac = Library(id="1", name="Movies", kind="movie",
                  paths=["/Volumes/Macintosh HD/Volumes/Public/Media/Movies"])  # fmt: skip
    got = place([mac], "/Volumes/Public/Media/Movies/Dune (2021)")
    assert got == (mac, "/Volumes/Macintosh HD/Volumes/Public/Media/Movies/Dune (2021)")
    caps = Library(id="2", name="TV", kind="series", paths=["/Volumes/Public/Media/tv"])
    assert place([caps], "/Volumes/Public/Media/TV/Severance") == (
        caps, "/Volumes/Public/Media/tv/Severance")  # fmt: skip
    exact = Library(id="3", name="TV 2", kind="series", paths=["/Volumes/Public/Media/TV"])
    assert covering([caps, exact], "/Volumes/Public/Media/TV/Severance") is exact
    assert covering([mac], "/volume1/Public/Media/Movies") is None  # a NAS is elsewhere
    assert same_path("/Volumes/Macintosh HD/A/b/", "/a/B")
    assert not same_path("/Volumes/Data/A", "/A")
