"""A title's own folder (show, movie, artist): remembered, found on disk, reused.

New files join the folder a title already has (one found when importing an existing
library, chosen when adding it, or where its first file went), whatever the naming says;
existing season folders are reused. The naming only names folders that don't exist yet.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import httpx

from fetchbox.modules.library.music_files import Naming, WantedTrack, track_path
from fetchbox.modules.library.naming import (
    DEFAULT_PATTERNS,
    episode_path,
    existing_season_folder,
    season_of_folder,
)
from fetchbox.modules.library.title_folders import folder_choices
from fetchbox.providers.download_clients import DownloadClientError
from fetchbox.providers.download_clients.builtin import BuiltinClient
from tests.conftest import Running
from tests.fake_torznab import Item
from tests.pipeline import Fakes, _indexer, standard_hd
from tests.test_existing_library import _dest, _job, _write
from tests.test_tv_import import PACK, fast, made, seed  # noqa: F401
from tests.torrents import Made, until

SEVERANCE = 95396  # tvdb 371980 in the fake TMDB


def test_season_folder_names() -> None:
    for name in ("Season 1", "Season 01", "season.1", "S01", "s1", "Series 1", "Staffel 1"):
        assert season_of_folder(name) == 1, name
    assert season_of_folder("Specials") == 0 and season_of_folder("Season 12") == 12
    assert season_of_folder("Extras") == 0 and season_of_folder("Behind the scenes") is None
    assert season_of_folder("Season 1 Disc 2") is None


def test_an_existing_season_folder_is_reused() -> None:
    assert existing_season_folder(["Season 1", "Season 2"], False, 1) == "Season 1"
    assert existing_season_folder(["Season 1"], False, 2) is None  # new: the naming's
    assert existing_season_folder([], True, 3) == ""  # loose episodes stay loose
    assert existing_season_folder(["Season 1"], True, 2) is None  # has season folders
    assert existing_season_folder(["Featurettes"], False, 1) is None


def test_paths_with_a_remembered_folder() -> None:
    ctx = {"Title": "Severance", "Year": 2022, "TvdbId": 371980, "Season": 1, "Episode": 3,
           "EpisodeTitle": "In Perpetuity", "Quality": "WEBDL-1080p"}  # fmt: skip
    p = DEFAULT_PATTERNS
    assert episode_path(p, ctx, ".mkv").startswith("Severance (2022) {tvdb-371980}/Season 01/")
    assert episode_path(p, ctx, ".mkv", folder="Severance", season_folder="Season 1") == (
        "Severance/Season 1/Severance - S01E03 - In Perpetuity [WEBDL-1080p].mkv"
    )
    assert episode_path(p, ctx, ".mkv", folder="Severance", season_folder="").startswith(
        "Severance/Severance - S01E03"
    )
    t = WantedTrack("t", 1, 3, "Roads", 300_000)
    assert track_path("Portishead", "Dummy", 1994, t, 1, ".flac", Naming(),
                      artist_folder="Portishead (UK)") == (
        "Portishead (UK)/Dummy (1994)/03 - Roads.flac"
    )  # fmt: skip


def test_folders_that_belong_to_a_title(tmp_path: Path) -> None:
    for name in ("Severance", "Severance (2022) {tvdb-371980}", "Severance (2010)",
                 "Severance (2022) {tvdb-1}", "Severed", ".Severance"):  # fmt: skip
        (tmp_path / name).mkdir()
    _write(tmp_path, "Severance/Season 1/Severance.S01E01.mkv")
    _write(tmp_path, "Severance/Season 2/Severance.S02E01.mkv")
    _write(tmp_path, "Severance/Season 2/Severance.S02E01.sample.mkv")
    got = folder_choices(tmp_path, "series", name="Severance", year=2022, tvdb_id=371980)
    assert [(c["name"], c["match"]) for c in got] == [
        ("Severance (2022) {tvdb-371980}", "id"),  # the id first
        ("Severance", "title"),  # no year in the name: still it
    ]  # another year, another id, another title, hidden: not it
    assert got[1]["files"] == 2 and got[1]["seasons"] == 2


async def test_the_add_dialog_lists_folders_already_there(c: httpx.AsyncClient) -> None:
    root = Path((await _dest(c, "series"))["path"])
    _write(root, "Severance/Season 1/Severance.S01E01.mkv")
    r = await c.get("/library/folder-choices",
                    params={"kind": "series", "name": "Severance", "year": 2022,
                            "tmdb_id": SEVERANCE})  # fmt: skip
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["library_folder"] == str(root)
    assert out["suggested"] == "Severance (2022) {tvdb-371980}"  # the TVDB id from details
    assert out["choices"] == [{"name": "Severance", "match": "title", "files": 1, "seasons": 1}]
    # another library folder: looked at there
    other = (
        await c.get("/library/folder-choices",
                    params={"kind": "series", "name": "Severance", "folder": str(root / "x")})
    ).json()  # fmt: skip
    assert other["library_folder"] == str(root / "x") and other["choices"] == []
    # a folder name that isn't one is refused when adding
    body = {"kind": "series", "tmdb_id": SEVERANCE, "profile_id": await standard_hd(c),
            "search": False, "title_folder": "../Elsewhere"}  # fmt: skip
    r = await c.post("/titles", json=body)
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "body.title_folder"


async def test_new_episodes_join_the_show_folder_already_there(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,  # noqa: F811
    seed: BuiltinClient,  # noqa: F811
) -> None:
    """The gate: a show already on disk as "Severance/Season 1" gets its new episodes
    there, not in "Severance (2022) {tvdb-371980}/Season 01"."""
    root = Path((await _dest(c, "series"))["path"])
    _write(root, "Severance/Season 1/Severance.S01E09.mkv")  # one episode already there
    await _indexer(c, fakes, "one", [Item(PACK, magnet=made.magnet, infohash=made.info_hash,
                                         size=2_000_000_000, category=5040)])  # fmt: skip
    body = {"kind": "series", "tmdb_id": SEVERANCE, "monitor_mode": "all", "search": False,
            "profile_id": await standard_hd(c), "title_folder": "Severance"}  # fmt: skip
    t = (await c.post("/titles", json=body)).json()
    where = (await c.get(f"/titles/{t['id']}/folder")).json()
    assert where["remembered"] and where["path"] == str(root / "Severance")
    res = (await c.get("/releases", params={"title_id": t["id"], "season": 1})).json()
    [rel] = [x for x in res["releases"] if x["candidate"]["title"] == PACK]
    assert (await c.post("/grabs", json={"release_id": rel["release_id"]})).status_code == 201
    engine = next(x for x in server.app.state.downloads.clients.values()
                  if isinstance(x, BuiltinClient))  # fmt: skip

    async def imported() -> bool:
        with contextlib.suppress(DownloadClientError):
            engine.connect_peer(made.info_hash, "127.0.0.1", seed.listen_port or 0)
        imports = (await c.get("/imports")).json()
        return bool(imports) and imports[0]["status"] == "imported"

    await until(imported, within=90)
    files = sorted(f["relative_path"] for f in (await c.get(f"/titles/{t['id']}/files")).json())
    assert files and all(f.startswith("Severance/Season 1/Severance - S01E0") for f in files), files
    assert not (root / "Severance (2022) {tvdb-371980}").exists()  # no second show folder
    assert not (root / "Severance" / "Season 01").exists()  # no second season folder


async def _import_existing(c: httpx.AsyncClient, root: Path, dest: dict[str, Any]) -> str:
    scan = (await _job(c, (await c.post("/library/scan",
            json={"destination_id": dest["id"]})).json()["id"]))["result"]  # fmt: skip
    [item] = scan["items"]
    one = {
        "folder": item["folder"],
        "files": [f["path"] for f in item["files"]],
        "tmdb_id": SEVERANCE,
        "profile_id": await standard_hd(c),
        "monitor_mode": "all",
    }
    body = {"destination_id": dest["id"], "items": [one]}
    res = (await _job(c, (await c.post("/library/import", json=body)).json()["id"]))["result"]
    assert res["files"] == 2, res
    return str(res["added"][0]["title_id"])


async def test_an_imported_show_keeps_its_folder_and_can_move(c: httpx.AsyncClient) -> None:
    dest = await _dest(c, "series")
    root = Path(dest["path"])
    _write(root, "Severance/Season 1/Severance.S01E01.1080p.WEB-DL-NTb.mkv")
    _write(root, "Severance/Season 1/Severance.S01E02.1080p.WEB-DL-NTb.mkv")
    _write(root, "Severance/Season 1/Severance.S01E02.1080p.WEB-DL-NTb.en.srt", 1024)
    _write(root, "Severance/poster.jpg", 1024)
    tid = await _import_existing(c, root, dest)
    where = (await c.get(f"/titles/{tid}/folder")).json()
    assert where["remembered"] and where["path"] == str(root / "Severance")  # found there

    # moved, with what Fetchbox didn't record (subtitles, artwork)
    new = root / "Severance (2022)"
    r = await c.put(f"/titles/{tid}/folder", json={"path": str(new)})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == 4 and r.json()["failed"] == []
    assert not (root / "Severance").exists()  # the old folder is gone
    assert (new / "poster.jpg").is_file()
    assert (new / "Season 1" / "Severance.S01E02.1080p.WEB-DL-NTb.en.srt").is_file()
    files = sorted(f["relative_path"] for f in (await c.get(f"/titles/{tid}/files")).json())
    assert files == [
        "Severance (2022)/Season 1/Severance.S01E01.1080p.WEB-DL-NTb.mkv",
        "Severance (2022)/Season 1/Severance.S01E02.1080p.WEB-DL-NTb.mkv",
    ]
    assert (await c.get(f"/titles/{tid}/folder")).json()["path"] == str(new)

    # only pointed elsewhere: the files stay
    elsewhere = root / "Sev"
    r = await c.put(f"/titles/{tid}/folder", json={"path": str(elsewhere), "move": False})
    assert r.status_code == 200 and r.json()["moved"] == 0
    assert (new / "poster.jpg").is_file() and not elsewhere.exists()
    assert (await c.get(f"/titles/{tid}/folder")).json()["path"] == str(elsewhere)

    # Rename existing files: the naming's folder, which the show then keeps
    allow = {"storage": {"rename_existing": True}}
    assert (await c.patch("/system/config", json=allow)).status_code == 200
    r = await c.post("/library/rename/series", json={"file_ids": None})
    assert r.status_code == 200 and len(r.json()["renamed"]) == 2, r.text
    named = root / "Severance (2022) {tvdb-371980}"
    assert (await c.get(f"/titles/{tid}/folder")).json()["path"] == str(named)

    # a path that can't be a folder of its own
    r = await c.put(f"/titles/{tid}/folder", json={"path": "relative/path"})
    assert r.status_code == 422


def test_an_unknown_id_leaves_no_empty_tag() -> None:
    from fetchbox.modules.library.naming import render

    pattern = DEFAULT_PATTERNS["series_folder"]
    assert render(pattern, {"Title": "Severance", "Year": 2022}) == "Severance (2022)"
    assert render(pattern, {"Title": "Severance", "Year": 2022, "TvdbId": 1}) == (
        "Severance (2022) {tvdb-1}"
    )


async def test_the_shows_own_folder_typed_as_the_library_folder(c: httpx.AsyncClient) -> None:
    root = Path((await _dest(c, "series"))["path"])
    _write(root, "Severance/Season 1/Severance.S01E01.mkv")
    _write(root, "Severance/Season 2/Severance.S02E01.mkv")
    r = await c.get("/library/folder-choices",
                    params={"kind": "series", "name": "Severance", "year": 2022,
                            "tmdb_id": SEVERANCE, "folder": str(root / "Severance")})  # fmt: skip
    out = r.json()
    assert out["choices"] == []  # nothing in it is the show: it is the show
    assert out["own"] == {"name": "Severance", "match": "title", "files": 2, "seasons": 2,
                          "library_folder": str(root)}  # fmt: skip
    # the library folder itself: no hint
    out = (await c.get("/library/folder-choices",
                       params={"kind": "series", "name": "Severance"})).json()  # fmt: skip
    assert out["own"] is None and out["choices"][0]["name"] == "Severance"
