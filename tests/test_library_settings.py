"""Library settings: naming per kind (presets, season folders), previews, renaming files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from fetchbox.providers.download_clients.builtin import BuiltinClient
from tests.conftest import Running
from tests.pipeline import Fakes, standard_hd
from tests.test_library_api import EXPECTED, NAME, _acquire, fast, made, seed  # noqa: F401
from tests.torrents import Made


async def _options(c: httpx.AsyncClient) -> dict[str, Any]:
    out: dict[str, Any] = (await c.get("/library/naming-options")).json()
    return out


async def test_naming_defaults_presets_and_preview(c: httpx.AsyncClient) -> None:
    opts = await _options(c)
    assert [p["id"] for p in opts["presets"]] == ["plex", "kodi", "simple"]
    assert [s["label"] for s in opts["season_folders"]][-1] == "No season folders"
    movie = (await c.get("/library/naming/movie")).json()
    assert movie["preset"] == "plex" and set(movie["patterns"]) == {"movie_folder", "movie_file"}
    assert movie["folder"]["path"].endswith("Movies")
    paths = [s["path"] for s in movie["samples"]]
    assert "Blade Runner - The Final Cut (1982)" in paths[1]  # ':' and '?' made safe
    assert paths[2] == "Untitled Archive Film {tmdb-1001}/Untitled Archive Film.mkv"  # no ()
    tv = (await c.get("/library/naming/series")).json()
    assert tv["patterns"]["season_folder"] == "Season {Season:00}"
    assert "/Season 00/" in tv["samples"][2]["path"]  # specials

    # preview without saving: no season folders
    r = await c.post("/library/naming/series/preview", json={"patterns": {"season_folder": ""}})
    assert r.status_code == 200, r.text
    assert r.json()["samples"][0]["path"] == (
        "Severance (2022) {tvdb-371980}/Severance - S01E07 - Defiant Jazz [WEBDL-1080p].mkv"
    )
    assert (await c.get("/library/naming/series")).json()["patterns"]["season_folder"] != ""

    # mistakes are explained
    r = await c.post(
        "/library/naming/series/preview", json={"patterns": {"episode_file": "{Title}"}}
    )
    assert r.status_code == 422 and "{Episode}" in r.text
    r = await c.put("/library/naming/movie", json={"patterns": {"movie_file": "{Nope}"}})
    assert r.status_code == 422 and "unknown token" in r.text

    # saving movie naming leaves TV alone, and the other way round
    kodi = next(p for p in opts["presets"] if p["id"] == "kodi")["patterns"]
    r = await c.put("/library/naming/movie", json={"patterns": kodi})
    assert r.status_code == 200 and r.json()["preset"] == "kodi"
    assert (await c.get("/library/naming/series")).json()["preset"] == "plex"
    r = await c.put("/library/naming/series", json={"patterns": {"season_folder": "S{Season:00}"}})
    assert r.json()["preset"] == "plex" and "/S01/" in r.json()["samples"][0]["path"]
    assert (await c.get("/library/naming/movie")).json()["preset"] == "kodi"
    custom = {"movie_file": "{Title} [{Resolution}]"}
    assert (await c.put("/library/naming/movie", json={"patterns": custom})).json()["preset"] == (
        "custom"
    )


async def test_rename_an_imported_movie(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,  # noqa: F811
    seed: BuiltinClient,  # noqa: F811
    settings: Any,
) -> None:
    movie = await _acquire(c, fakes, server, made, seed)
    assert (await c.get("/library/rename/movie")).json() == []  # already named this way

    simple = next(p for p in (await _options(c))["presets"] if p["id"] == "simple")["patterns"]
    assert (await c.put("/library/naming/movie", json={"patterns": simple})).status_code == 200
    [step] = (await c.get("/library/rename/movie")).json()
    assert step["current"] == EXPECTED and step["new"] == "Dune (2021)/Dune (2021).mkv"
    assert step["title_id"] == movie["id"]

    r = await c.post("/library/rename/movie", json={})
    assert r.status_code == 200, r.text
    assert [x["new"] for x in r.json()["renamed"]] == [step["new"]] and r.json()["failed"] == []
    library = settings.downloads_dir / "Movies"
    new = library / "Dune (2021)" / "Dune (2021).mkv"
    assert new.exists() and not (library / EXPECTED).exists()
    assert not (library / EXPECTED).parent.exists()  # the emptied folder went too
    seeding = settings.downloads_dir / "Torrents" / "fetchbox-movies" / NAME / f"{NAME}.mkv"
    assert os.stat(new).st_ino == os.stat(seeding).st_ino  # still the same hardlinked file
    [f] = (await c.get(f"/titles/{movie['id']}/files")).json()
    assert f["relative_path"] == step["new"]
    assert (await c.get("/library/rename/movie")).json() == []


async def test_rename_episode_files(c: httpx.AsyncClient, fakes: Fakes, server: Running) -> None:
    from fetchbox.modules.library.models import MediaFile, MediaFileItem

    body = {"kind": "series", "tmdb_id": 95396, "monitor_mode": "all",
            "profile_id": await standard_hd(c), "search": False}  # fmt: skip
    t = (await c.post("/titles", json=body)).json()
    eps = (await c.get(f"/titles/{t['id']}/episodes")).json()[0]["episodes"]
    e5, e6, e7 = (eps[n]["wanted"]["id"] for n in (4, 5, 6))
    dest = await server.app.state.storage.default_destination("series")
    root = Path(dest.path)
    old = {
        "messy/Severance.S01E05E06.mkv": ([e5, e6], "Severance.S01E05E06.1080p.WEB-DL-NTb.mkv"),
        "messy/s01e07.mkv": ([e7], "Severance.S01E07.720p.HDTV.x264-GRP.mkv"),
    }
    async with server.app.state.fb.db.sessionmaker() as s:
        for rel, (items, original) in old.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(b"x")
            quality = "WEBDL-1080p" if "1080p" in original else "HDTV-720p"
            mf = MediaFile(storage_node_id=dest.node_id, destination_id=dest.id,
                           relative_path=rel, size=1, quality={"name": quality},
                           original_name=original)  # fmt: skip
            s.add(mf)
            await s.flush()
            s.add_all([MediaFileItem(media_file_id=mf.id, wanted_item_id=w) for w in items])
        await s.commit()
    # something already where E07 would go: that one fails, the rest still move
    base = "Severance (2022) {tvdb-371980}/Season 01/"
    blocker = root / base / "Severance - S01E07 - Ep 7 [HDTV-720p].mkv"
    blocker.parent.mkdir(parents=True)
    blocker.write_bytes(b"other")

    plan = {x["current"]: x["new"] for x in (await c.get("/library/rename/series")).json()}
    assert plan == {
        "messy/Severance.S01E05E06.mkv": base
        + "Severance - S01E05-E06 - Ep 5 + Ep 6 [WEBDL-1080p].mkv",
        "messy/s01e07.mkv": base + "Severance - S01E07 - Ep 7 [HDTV-720p].mkv",
    }  # fmt: skip
    out = (await c.post("/library/rename/series", json={})).json()
    assert [x["current"] for x in out["renamed"]] == ["messy/Severance.S01E05E06.mkv"]
    assert [x["current"] for x in out["failed"]] == ["messy/s01e07.mkv"]
    assert "already exists" in out["failed"][0]["error"]
    assert (root / "messy/s01e07.mkv").exists() and blocker.read_bytes() == b"other"

    # only the listed files
    blocker.unlink()
    r = await c.post("/library/rename/series", json={"file_ids": ["nope"]})
    assert r.json() == {"renamed": [], "failed": []}
    out = (await c.post("/library/rename/series", json={})).json()
    assert len(out["renamed"]) == 1 and not (root / "messy").exists()
