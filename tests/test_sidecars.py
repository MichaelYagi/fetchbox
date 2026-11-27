"""Sidecar files (added by plugins): beside their media file, and moved, recycled and
deleted with it. With none added, nothing about a file's handling changes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from fetchbox.modules.library.sidecars import SidecarError, check_suffix, sidecar_rel
from fetchbox.providers.download_clients.builtin import BuiltinClient
from tests.conftest import Running
from tests.pipeline import Fakes
from tests.test_library_api import EXPECTED, _acquire, fast, made, seed  # noqa: F401
from tests.torrents import Made

SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"


@pytest.mark.parametrize("ok", ["srt", "en.srt", "forced.en.srt", "nfo", "poster.jpg", "pt-BR.srt"])
def test_good_suffixes(ok: str) -> None:
    assert check_suffix(ok) == ok


@pytest.mark.parametrize(
    "bad", ["", ".srt", "../x.srt", "a/b.srt", "a\\b.srt", "en.mkv", "x.FLAC", "en.mp4", "a..srt"]
)
def test_bad_suffixes(bad: str) -> None:
    with pytest.raises(SidecarError):
        check_suffix(bad)


def test_a_sidecar_sits_beside_its_owner() -> None:
    assert sidecar_rel("Dune (2021)/Dune (2021) [WEBDL-1080p].mkv", "en.srt") == (
        "Dune (2021)/Dune (2021) [WEBDL-1080p].en.srt"
    )
    assert sidecar_rel("Show/Season 01/Show - S01E01.mkv", "nfo") == (
        "Show/Season 01/Show - S01E01.nfo"
    )


async def test_a_sidecar_follows_its_file(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,  # noqa: F811
    seed: BuiltinClient,  # noqa: F811
    settings: Any,
    tmp_path: Path,
) -> None:
    library: Any = server.app.state.library
    movie = await _acquire(c, fakes, server, made, seed)
    [f] = (await c.get(f"/titles/{movie['id']}/files")).json()
    root = settings.downloads_dir / "Movies"
    assert f["relative_path"] == EXPECTED

    row = await library.add_sidecar(f["id"], "en.srt", SRT, plugin="subtitles", meta={"by": "t"})
    srt = root / sidecar_rel(EXPECTED, "en.srt")
    assert srt.read_bytes() == SRT and row.plugin == "subtitles"
    assert [x.suffix for x in await library.sidecars(f["id"])] == ["en.srt"]
    # written again: replaced, still one
    await library.add_sidecar(f["id"], "en.srt", b"new", plugin="subtitles")
    assert srt.read_bytes() == b"new" and len(await library.sidecars(f["id"])) == 1

    # renamed by the naming: the sidecar takes the new name too, the old folder goes
    simple = {"movie_folder": "{Title} ({Year})", "movie_file": "{Title} ({Year})"}
    assert (await c.put("/library/naming/movie", json={"patterns": simple})).status_code == 200
    assert (await c.patch("/system/config", json={"storage": {"rename_existing": True}})).is_success
    r = await c.post("/library/rename/movie", json={})
    assert r.status_code == 200 and r.json()["failed"] == [], r.text
    now = "Dune (2021)/Dune (2021).mkv"
    assert (root / now).exists() and (
        root / "Dune (2021)/Dune (2021).en.srt"
    ).read_bytes() == b"new"
    assert not srt.exists() and not srt.parent.exists()

    # the title moves to another folder: the sidecar comes along
    elsewhere = tmp_path / "Elsewhere" / "Dune (2021)"
    r = await c.put(f"/titles/{movie['id']}/folder", json={"path": str(elsewhere)})
    assert r.status_code == 200, r.text
    [f] = (await c.get(f"/titles/{movie['id']}/files")).json()
    assert (elsewhere / Path(f["relative_path"]).name).exists()
    assert (elsewhere / "Dune (2021).en.srt").read_bytes() == b"new"

    # the file is deleted: its sidecar goes to the recycle folder with it, the record too
    assert (await c.delete(f"/files/{f['id']}")).status_code == 204
    assert not (elsewhere / "Dune (2021).en.srt").exists()
    recycled = [p.name for p in tmp_path.joinpath("Elsewhere").rglob("*.en.srt")]
    assert recycled == ["Dune (2021).en.srt"], recycled  # in the recycle folder
    assert await library.sidecars(f["id"]) == []


async def test_removing_a_sidecar_and_refusing_bad_ones(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,  # noqa: F811
    seed: BuiltinClient,  # noqa: F811
    settings: Any,
) -> None:
    library: Any = server.app.state.library
    movie = await _acquire(c, fakes, server, made, seed)
    [f] = (await c.get(f"/titles/{movie['id']}/files")).json()
    with pytest.raises(SidecarError):
        await library.add_sidecar(f["id"], "../../escape.srt", SRT, plugin="x")
    with pytest.raises(SidecarError):
        await library.add_sidecar(f["id"], "fake.mkv", SRT, plugin="x")
    await library.add_sidecar(f["id"], "nfo", b"<movie/>", plugin="kodi")
    nfo = settings.downloads_dir / "Movies" / sidecar_rel(EXPECTED, "nfo")
    assert nfo.exists()
    # listed with its file, and in the title's history
    [listed] = (await c.get(f"/titles/{movie['id']}/files")).json()
    [sc] = listed["sidecars"]
    assert (sc["suffix"], sc["plugin"], sc["relative_path"]) == (
        "nfo",
        "kodi",
        sidecar_rel(EXPECTED, "nfo"),
    )
    assert await library.remove_sidecar(f["id"], "nfo") and not nfo.exists()
    assert (await c.get(f"/titles/{movie['id']}/files")).json()[0]["sidecars"] == []
    history = (await c.get(f"/titles/{movie['id']}/history", params={"limit": 50})).json()
    rows = history.get("items", history)
    kinds = [(h["type"], h["data"].get("plugin")) for h in rows if h["type"].startswith("sidecar")]
    assert ("sidecar_added", "kodi") in kinds and ("sidecar_removed", "kodi") in kinds
    assert not await library.remove_sidecar(f["id"], "nfo")
    assert (settings.downloads_dir / "Movies" / EXPECTED).exists()  # the movie is untouched
