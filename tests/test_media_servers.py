"""Media servers: Plex / Jellyfin / Emby are told to rescan the folders Fetchbox changed.

The Jellyfin fake follows what a real Jellyfin 12.4 returned (VirtualFolders shape,
204 for /Library/Media/Updated, 401 for a wrong key, MediaBrowser Token auth); Plex
follows its documented /library/sections API.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from fetchbox.core.secrets import MASK
from fetchbox.modules.integrations.service import (
    PathMapping,
    guess_mappings,
    learn_mappings,
    to_remote,
)
from fetchbox.providers.media_servers import Library, covering
from tests.conftest import Running
from tests.pipeline import Fakes, standard_hd
from tests.torrents import until

JF_KEY = "0123456789abcdef0123456789abcdef"
PLEX_TOKEN = "plex-token-123"


class FakeServers:
    def __init__(self) -> None:
        self.updated: list[list[str]] = []  # Jellyfin /Library/Media/Updated paths
        self.plex_refreshed: list[tuple[str, str]] = []  # (section, path)
        self.plex_libs = [{"key": "1", "title": "Films", "type": "movie",
                           "Location": [{"id": 1, "path": "/data/movies"}]}]  # fmt: skip
        self.plex_files: list[str] = []  # what /library/sections/*/all lists
        self.plex_dirs: dict[str, list[str]] = {}  # folder -> what Plex's browser shows in it
        self.down = False

    def handle(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=req)
        if req.url.host == "jellyfin":
            if f'Token="{JF_KEY}"' not in req.headers.get("authorization", ""):
                return httpx.Response(401)
            if req.url.path == "/System/Info":
                return httpx.Response(200, json={"ServerName": "den", "Version": "12.4.0"})
            if req.url.path == "/Library/VirtualFolders":
                return httpx.Response(200, json=[
                    {"Name": "Movies", "CollectionType": "movies", "Locations": ["/media/movies"],
                     "ItemId": "m1"},
                    {"Name": "Shows", "CollectionType": "tvshows", "Locations": ["/media/tv"],
                     "ItemId": "t1"},
                ])  # fmt: skip
            if req.url.path == "/Library/Media/Updated" and req.method == "POST":
                self.updated.append([u["Path"] for u in json.loads(req.content)["Updates"]])
                return httpx.Response(204)
        if req.url.host == "plex":
            if req.headers.get("x-plex-token") != PLEX_TOKEN:
                return httpx.Response(401)
            if req.url.path == "/library/sections":
                return httpx.Response(200, json={"MediaContainer": {"Directory": self.plex_libs}})
            if req.url.path.endswith("/all"):
                items = [{"Media": [{"Part": [{"file": f}]}]} for f in self.plex_files]
                return httpx.Response(200, json={"MediaContainer": {"Metadata": items}})
            if req.url.path.startswith("/services/browse/"):
                folder = base64.b64decode(req.url.path.rsplit("/", 1)[1]).decode()
                if folder not in self.plex_dirs:
                    return httpx.Response(404)
                paths = [{"path": p} for p in self.plex_dirs[folder]]
                return httpx.Response(200, json={"MediaContainer": {"Path": paths}})
            if req.url.path.startswith("/library/sections/") and req.url.path.endswith("/refresh"):
                self.plex_refreshed.append((req.url.path.split("/")[3], req.url.params["path"]))
                return httpx.Response(200)
        return httpx.Response(404)


@pytest.fixture
def servers(server: Running) -> FakeServers:
    f = FakeServers()
    svc = server.app.state.integrations
    svc.provider_kwargs = {"transport": httpx.MockTransport(f.handle)}
    svc.gather_seconds = 0.2
    return f


def test_path_helpers() -> None:
    maps = [PathMapping(local="/home/me/Media", remote="/media"),
            PathMapping(local="/home/me/Media/TV", remote="/tv")]  # fmt: skip
    assert to_remote("/home/me/Media/Movies/Dune (2021)", maps) == "/media/Movies/Dune (2021)"
    assert to_remote("/home/me/Media/TV/Show", maps) == "/tv/Show"  # longest wins
    assert to_remote("/elsewhere/x", maps) == "/elsewhere/x"
    win = [PathMapping(local="/mnt/d/Movies", remote="D:\\Movies")]
    assert to_remote("/mnt/d/Movies/Dune (2021)", win) == "D:\\Movies\\Dune (2021)"
    libs = [Library(id="1", name="A", kind="movie", paths=["/m"]),
            Library(id="2", name="B", kind="movie", paths=["/m/kids"])]  # fmt: skip
    assert covering(libs, "/m/kids/Film").name == "B"  # type: ignore[union-attr]
    assert covering(libs, "/mx/Film") is None


def test_the_servers_path_from_files_both_know() -> None:
    mac = "/Volumes/Public/Media/Partition_2/Movies"
    song = "Weyes Blood/Titanic Rising (2019)/01 - Everyday.flac"
    local = [(mac, "Dune (2021)/Dune (2021).mkv"), (mac, "Heat (1995)/Heat (1995).mkv"),
             ("/Volumes/Public/Music", song)]  # fmt: skip
    nas = ["/volume1/Public/Media/Partition_2/Movies/Dune (2021)/Dune (2021).mkv",
           "/volume1/Public/Media/Partition_2/Movies/Heat (1995)/Heat (1995).mkv",
           "/volume1/Public/Media/Partition_2/Movies/Other (2020)/Other (2020).mkv",
           f"/share/music/{song}"]  # fmt: skip
    assert learn_mappings(local, nas) == [
        PathMapping(local=mac, remote="/volume1/Public/Media/Partition_2/Movies"),
        PathMapping(local="/Volumes/Public/Music", remote="/share/music"),
    ]
    # Plex on Windows, and a name no folder shares (here /data/films vs Movies)
    assert learn_mappings([(mac, "Dune (2021)/Dune (2021).mkv")],
                          ["D:\\films\\Dune (2021)\\Dune (2021).mkv"]) == [
        PathMapping(local=mac, remote="D:\\films")]  # fmt: skip
    # the same file at a different place inside: not the same file
    assert learn_mappings([(mac, "Dune (2021)/Dune (2021).mkv")],
                          ["/nas/Dune (2021) extras/Dune (2021).mkv"]) == []  # fmt: skip
    # the same machine: nothing to map
    assert learn_mappings([(mac, "Dune (2021)/Dune (2021).mkv")],
                          [f"{mac}/Dune (2021)/Dune (2021).mkv"]) == []  # fmt: skip


def test_the_servers_path_from_folder_names() -> None:
    nas = [Library(id="1", name="Movies", kind="movie",
                   paths=["/volume1/Public/Media/Partition_2/Movies"])]  # fmt: skip
    film = "/Volumes/Public/Media/Partition_2/Movies/You Can See Everything (2026)"
    [m] = guess_mappings(film, nas)
    assert to_remote(film, [m]) == (
        "/volume1/Public/Media/Partition_2/Movies/You Can See Everything (2026)"
    )
    win = [Library(id="1", name="Movies", kind="movie", paths=["D:\\Media\\Movies"])]
    [m] = guess_mappings("/Volumes/D/Media/Movies/Dune (2021)", win)
    assert to_remote("/Volumes/D/Media/Movies/Dune (2021)", [m]) == "D:\\Media\\Movies\\Dune (2021)"
    two = [Library(id="1", name="A", kind="movie", paths=["/a/Movies"]),
           Library(id="2", name="B", kind="movie", paths=["/b/Movies"])]  # fmt: skip
    assert len(guess_mappings("/Volumes/x/Movies/Dune", two)) == 2  # the server picks
    assert guess_mappings("/srv/Films/Dune", nas) == []  # nothing in common


async def _add(c: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    r = await c.post("/media-servers", json=body)
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


async def test_connect_test_and_coverage(
    c: httpx.AsyncClient, servers: FakeServers, settings: Any
) -> None:
    movies = str(settings.downloads_dir / "Movies")
    jf = await _add(c, {"kind": "jellyfin", "name": "Den", "settings": {
        "url": "http://jellyfin:8096/", "api_key": f" {JF_KEY}\n"},
        "path_mappings": [{"local": movies, "remote": "/media/movies"}]})  # fmt: skip
    assert jf["settings"]["api_key"] == MASK and jf["settings"]["url"] == "http://jellyfin:8096"
    r = (await c.post(f"/media-servers/{jf['id']}/test")).json()
    assert r["ok"] and r["message"] == "Jellyfin 12.4.0 works (den): 2 libraries"
    cov = {x["local"]: x for x in r["coverage"]}
    assert cov[movies]["library"] == "Movies" and cov[movies]["remote"] == "/media/movies"
    # TV isn't mapped, but Jellyfin's /media/tv ends the same way: found by itself
    tv = next(x for x in r["coverage"] if x["folder"] == "TV")
    assert tv["library"] == "Shows" and tv["remote"] == "/media/tv" and tv["found"]
    assert not cov[movies]["found"]  # that one came from the mapping

    bad = await _add(c, {"kind": "plex", "name": "Plex", "settings": {
        "url": "http://plex:32400", "token": "wrong"}})  # fmt: skip
    r = (await c.post(f"/media-servers/{bad['id']}/test")).json()
    assert not r["ok"] and r["message"] == "Plex refused the token"
    r = await c.patch(f"/media-servers/{bad['id']}",
                      json={"settings": {"url": "http://plex:32400", "token": PLEX_TOKEN}},
                      headers={"If-Match": bad["etag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    assert (await c.post(f"/media-servers/{bad['id']}/test")).json()["ok"]
    # sending the mask back keeps the token
    r = await c.patch(f"/media-servers/{bad['id']}",
                      json={"settings": {"url": "http://plex:32400/", "token": MASK}})  # fmt: skip
    assert r.status_code == 200 and (await c.post(f"/media-servers/{bad['id']}/test")).json()["ok"]

    r = await c.post("/media-servers", json={"kind": "kodi", "name": "x", "settings": {}})
    assert r.status_code == 422
    r = await c.post("/media-servers", json={"kind": "plex", "name": "x",
                                             "settings": {"url": "plex"}})  # fmt: skip
    assert r.status_code == 422
    assert sorted((await c.get("/media-servers/kinds")).json()) == ["emby", "jellyfin", "plex"]


async def test_imports_and_renames_trigger_one_refresh_each(
    c: httpx.AsyncClient, fakes: Fakes, servers: FakeServers, settings: Any
) -> None:
    movies = settings.downloads_dir / "Movies"
    await _add(c, {"kind": "jellyfin", "name": "Den", "settings": {
        "url": "http://jellyfin:8096", "api_key": JF_KEY},
        "path_mappings": [{"local": str(movies), "remote": "/media/movies"}]})  # fmt: skip
    await _add(c, {"kind": "plex", "name": "Plex", "settings": {
        "url": "http://plex:32400", "token": PLEX_TOKEN},
        "path_mappings": [{"local": str(movies), "remote": "/data/movies"}]})  # fmt: skip

    dune = "Dune (2021) {tmdb-438631}/Dune.2021.1080p.BluRay.x264-GRP.mkv"
    amelie = "Amélie (2001)/Amelie.2001.720p.BluRay.x264-GRP.mkv"
    for rel in (dune, amelie):
        p = movies / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * 1000)
    dest = next(d for d in (await c.get("/destinations")).json() if d["media_kind"] == "movie")
    profile = await standard_hd(c)
    items = [
        {"folder": dune.split("/")[0], "tmdb_id": 438631, "profile_id": profile, "files": [dune]},
        {"folder": amelie.split("/")[0], "tmdb_id": 194, "profile_id": profile, "files": [amelie]},
    ]
    r = await c.post("/library/import", json={"destination_id": dest["id"], "items": items})
    assert r.status_code == 202

    async def refreshed() -> bool:
        return bool(servers.updated) and len(servers.plex_refreshed) == 2

    await until(refreshed, within=15)
    assert servers.updated == [  # one call for both
        ["/media/movies/Amélie (2001)", "/media/movies/Dune (2021) {tmdb-438631}"]
    ]
    assert sorted(servers.plex_refreshed) == [
        ("1", "/data/movies/Amélie (2001)"),
        ("1", "/data/movies/Dune (2021) {tmdb-438631}"),
    ]

    async def recorded() -> bool:  # written just after the calls: wait for it
        rows = (await c.get("/media-servers")).json()
        return all(x["last_refresh_at"] for x in rows)

    await until(recorded, within=10)
    rows = (await c.get("/media-servers")).json()
    assert all(x["last_error"] is None for x in rows), rows

    # a rename: old and new folders are both rescanned
    servers.updated.clear()
    simple = {"movie_folder": "{Title} ({Year})", "movie_file": "{Title} ({Year})"}
    assert (await c.put("/library/naming/movie", json={"patterns": simple})).status_code == 200
    allow = {"storage": {"rename_existing": True}}
    assert (await c.patch("/system/config", json=allow)).status_code == 200
    out = (await c.post("/library/rename/movie", json={})).json()
    assert len(out["renamed"]) == 2

    async def renamed() -> bool:
        return bool(servers.updated)

    await until(renamed, within=15)
    assert set(servers.updated[0]) == {  # one call: old and new folders
        "/media/movies/Amélie (2001)",
        "/media/movies/Dune (2021)",
        "/media/movies/Dune (2021) {tmdb-438631}",
    }


async def test_refresh_problems_are_recorded_not_raised(
    c: httpx.AsyncClient, servers: FakeServers, server: Running
) -> None:
    row = await _add(c, {"kind": "jellyfin", "name": "Den", "settings": {
        "url": "http://jellyfin:8096", "api_key": JF_KEY}})  # fmt: skip
    svc = server.app.state.integrations
    await server.app.state.fb.bus.emit("import.completed", folders=["/not/in/any/library"])

    async def recorded() -> bool:
        x = (await c.get("/media-servers")).json()[0]
        return x["last_error"] is not None

    await until(recorded, within=10)
    err = (await c.get("/media-servers")).json()[0]["last_error"]
    assert "no Jellyfin library holds /not/in/any/library" in err and "path mapping" in err

    servers.down = True
    svc._pending.add("/media/movies/x")
    await svc.refresh_pending()
    err = (await c.get("/media-servers")).json()[0]["last_error"]
    assert err.startswith("Jellyfin unreachable at http://jellyfin:8096")
    assert (await c.delete(f"/media-servers/{row['id']}")).status_code == 204


async def test_plex_elsewhere_needs_no_mapping(
    c: httpx.AsyncClient, servers: FakeServers, server: Running, settings: Any
) -> None:
    # Plex (on a NAS) has /data/movies; Fetchbox's folder is .../Movies: no mapping set
    await _add(c, {"kind": "plex", "name": "Plex", "settings": {
        "url": "http://plex:32400", "token": PLEX_TOKEN}})  # fmt: skip
    film = settings.downloads_dir / "Movies" / "You Can See Everything (2026)"
    await server.app.state.fb.bus.emit("import.completed", folders=[str(film)])

    async def refreshed() -> bool:
        return bool(servers.plex_refreshed)

    await until(refreshed, within=10)
    assert servers.plex_refreshed == [("1", "/data/movies/You Can See Everything (2026)")]

    async def recorded() -> bool:
        return (await c.get("/media-servers")).json()[0]["last_refresh_at"] is not None

    await until(recorded, within=10)
    assert (await c.get("/media-servers")).json()[0]["last_error"] is None


async def _import_two(c: httpx.AsyncClient, movies: Any) -> tuple[str, str]:
    dune = "Dune (2021) {tmdb-438631}/Dune.2021.1080p.BluRay.x264-GRP.mkv"
    amelie = "Amélie (2001)/Amelie.2001.720p.BluRay.x264-GRP.mkv"
    for rel in (dune, amelie):
        p = movies / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * 1000)
    dest = next(d for d in (await c.get("/destinations")).json() if d["media_kind"] == "movie")
    profile = await standard_hd(c)
    items = [
        {"folder": dune.split("/")[0], "tmdb_id": 438631, "profile_id": profile, "files": [dune]},
        {"folder": amelie.split("/")[0], "tmdb_id": 194, "profile_id": profile, "files": [amelie]},
    ]
    r = await c.post("/library/import", json={"destination_id": dest["id"], "items": items})
    assert r.status_code == 202
    return dune, amelie


async def test_files_plex_has_tell_where_it_sees_the_folder(
    c: httpx.AsyncClient, fakes: Fakes, servers: FakeServers, server: Running, settings: Any
) -> None:
    # Plex's library is /nas/films: nothing like "Movies" to go by, and no mapping set
    movies = settings.downloads_dir / "Movies"
    servers.plex_libs[0]["Location"] = [{"id": 1, "path": "/nas/films"}]
    dune = "Dune (2021) {tmdb-438631}/Dune.2021.1080p.BluRay.x264-GRP.mkv"
    servers.plex_files = [f"/nas/films/{dune}"]  # Plex already has Dune
    plex = await _add(c, {"kind": "plex", "name": "Plex", "settings": {
        "url": "http://plex:32400", "token": PLEX_TOKEN}})  # fmt: skip
    await _import_two(c, movies)

    async def refreshed() -> bool:
        return len(servers.plex_refreshed) == 2

    await until(refreshed, within=15)
    assert sorted(servers.plex_refreshed) == [
        ("1", "/nas/films/Amélie (2001)"),
        ("1", "/nas/films/Dune (2021) {tmdb-438631}"),
    ]

    r = (await c.post(f"/media-servers/{plex['id']}/test")).json()
    [mov] = [x for x in r["coverage"] if x["local"] == str(movies)]
    assert mov["remote"] == "/nas/films" and mov["library"] == "Films" and mov["how"] == "files"
    assert mov["visible"] is None  # this Plex's browser doesn't answer: can't say

    # Plex's browser says Amélie's folder isn't there: reported, not scanned for nothing
    servers.plex_refreshed.clear()
    servers.plex_dirs["/nas/films"] = ["/nas/films/Dune (2021) {tmdb-438631}"]
    svc = server.app.state.integrations
    svc._pending.update({str(movies / "Amélie (2001)"), str(movies / "Dune (2021) {tmdb-438631}")})
    await svc.refresh_pending()
    assert servers.plex_refreshed == [("1", "/nas/films/Dune (2021) {tmdb-438631}")]
    err = (await c.get("/media-servers")).json()[0]["last_error"]
    assert "Plex can't see /nas/films/Amélie (2001)" in err
    servers.plex_dirs["/nas"] = ["/nas/films"]
    r = (await c.post(f"/media-servers/{plex['id']}/test")).json()
    assert next(x for x in r["coverage"] if x["local"] == str(movies))["visible"] is True


async def test_plex_picks_between_two_equally_named_folders(
    c: httpx.AsyncClient, servers: FakeServers, server: Running, settings: Any
) -> None:
    servers.plex_libs = [
        {"key": "1", "title": "A", "type": "movie", "Location": [{"id": 1, "path": "/a/Movies"}]},
        {"key": "2", "title": "B", "type": "movie", "Location": [{"id": 2, "path": "/b/Movies"}]},
    ]
    servers.plex_dirs = {"/a/Movies": [], "/b/Movies": ["/b/Movies/Heat (1995)"]}
    await _add(c, {"kind": "plex", "name": "Plex", "settings": {
        "url": "http://plex:32400", "token": PLEX_TOKEN}})  # fmt: skip
    film = settings.downloads_dir / "Movies" / "Heat (1995)"
    await server.app.state.fb.bus.emit("import.completed", folders=[str(film)])

    async def refreshed() -> bool:
        return bool(servers.plex_refreshed)

    await until(refreshed, within=10)
    assert servers.plex_refreshed == [("2", "/b/Movies/Heat (1995)")]


async def test_a_deleted_file_makes_plex_rescan_its_folder(
    c: httpx.AsyncClient, servers: FakeServers, server: Running, settings: Any
) -> None:
    """Deleting a file (from Fetchbox, off the disk) has Plex drop it: its folder rescans."""
    await _add(c, {"kind": "plex", "name": "Plex", "settings": {
        "url": "http://plex:32400", "token": PLEX_TOKEN}})  # fmt: skip
    film = settings.downloads_dir / "Movies" / "Dune (2021)"
    await server.app.state.fb.bus.emit("file.deleted", path="Dune (2021)/Dune.mkv",
                                       from_disk=True, folders=[str(film)])  # fmt: skip

    async def refreshed() -> bool:
        return bool(servers.plex_refreshed)

    await until(refreshed, within=10)
    assert servers.plex_refreshed == [("1", "/data/movies/Dune (2021)")]
