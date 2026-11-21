"""Media servers: Plex / Jellyfin / Emby are told to rescan the folders Fetchbox changed.

The Jellyfin fake follows what a real Jellyfin 12.4 returned (VirtualFolders shape,
204 for /Library/Media/Updated, 401 for a wrong key, MediaBrowser Token auth); Plex
follows its documented /library/sections API.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from fetchbox.core.secrets import MASK
from fetchbox.modules.integrations.service import PathMapping, to_remote
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
                return httpx.Response(200, json={"MediaContainer": {"Directory": [
                    {"key": "1", "title": "Films", "type": "movie",
                     "Location": [{"id": 1, "path": "/data/movies"}]},
                ]}})  # fmt: skip
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
    assert any(x["library"] is None for x in r["coverage"])  # TV isn't mapped: not covered

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
