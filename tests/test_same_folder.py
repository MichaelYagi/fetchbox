"""One folder written two ways (a symlink; on a Mac /Volumes/Macintosh HD/X is /X) is one
library folder: never a second "Movies 2", and old duplicates are merged."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

from fetchbox.core.events import Actor
from fetchbox.modules.library.models import MediaFile
from fetchbox.modules.media.models import Title
from tests.conftest import Running
from tests.pipeline import standard_hd

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin on Windows")


def _alias(tmp_path: Path) -> tuple[Path, Path]:
    real = tmp_path / "Media" / "Movies"
    real.mkdir(parents=True)
    os.symlink(tmp_path / "Media", tmp_path / "Disk")
    return real, tmp_path / "Disk" / "Movies"


async def test_the_same_folder_written_another_way_is_reused(
    client: httpx.AsyncClient, server: Running, tmp_path: Path
) -> None:
    c = client
    real, alias = _alias(tmp_path)
    r = await c.post("/destinations", json={"name": "Films", "path": str(real),
                                            "media_kind": "movie"})  # fmt: skip
    assert r.status_code == 201, r.text
    storage = server.app.state.storage
    got = await storage.folder_for("movie", str(alias), field="body.folder", actor=Actor())
    assert got == r.json()["id"]
    names = [d["name"] for d in (await c.get("/destinations")).json()]
    assert names.count("Films") == 1 and "Movies" in names and "Movies 2" not in names


async def test_duplicate_library_folders_are_merged(
    client: httpx.AsyncClient, server: Running, tmp_path: Path
) -> None:
    c = client
    real, alias = _alias(tmp_path)
    ids = []
    for name, path in (("Films", real), ("Films 2", alias)):
        r = await c.post("/destinations", json={"name": name, "path": str(path),
                                                "media_kind": "movie"})  # fmt: skip
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    keep, dup = ids
    profile = await standard_hd(c)
    db = server.app.state.storage.db
    async with db.sessionmaker() as s:
        node = (await c.get("/destinations")).json()[0]["node_id"]
        title = Title(kind="movie", tmdb_id=438631, name="Dune", sort_name="dune", year=2021,
                      profile_id=profile, destination_id=dup)  # fmt: skip
        s.add(title)
        await s.flush()
        s.add(MediaFile(storage_node_id=node, destination_id=dup, quality={},
                        relative_path="Dune (2021)/Dune.mkv", size=1))  # fmt: skip
        await s.commit()
        tid = title.id

    assert await server.app.state.storage.merge_duplicates() == ["Films 2"]
    names = [d["name"] for d in (await c.get("/destinations")).json()]
    assert "Films" in names and "Films 2" not in names
    async with db.sessionmaker() as s:
        assert (await s.get(Title, tid)).destination_id == keep  # type: ignore[union-attr]
        files = (await s.execute(MediaFile.__table__.select())).all()
        assert [f.destination_id for f in files] == [keep]
    assert await server.app.state.storage.merge_duplicates() == []
