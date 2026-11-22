"""Move from Radarr / Sonarr. The fake serves responses recorded from real Radarr
7.0.3 and Sonarr 4.0.20 (tests/fixtures/arr; local paths replaced by {ROOT})."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fetchbox.modules.library.migrate import guess_profile, translate
from tests.conftest import Running
from tests.pipeline import Fakes
from tests.test_existing_library import _job

FIXTURES = Path(__file__).parent / "fixtures" / "arr"
KEY = "arr-api-key-123"


def fake_arr(root: Path) -> Any:
    data = {
        port: json.loads((FIXTURES / f"{kind}.json").read_text().replace("{ROOT}", root.as_posix()))
        for kind, port in (("radarr", 7878), ("sonarr", 8989))
    }

    def handle(req: httpx.Request) -> httpx.Response:
        if req.headers.get("x-api-key") != KEY:
            return httpx.Response(401)
        path = req.url.path.removeprefix("/api/v3")
        if req.url.query:
            path += "?" + req.url.query.decode()
        body = data[req.url.port].get(path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    return httpx.MockTransport(handle)


@pytest.fixture
def media(tmp_path: Path) -> Path:
    root = tmp_path / "arrmedia"
    for rel in ("movies/Dune (2021)/Dune.2021.1080p.BluRay.x264-GRP.mkv",
                "tv/Severance/Season 01/Severance.S01E01.1080p.WEB-DL-NTb.mkv",
                "tv/Severance/Season 01/Severance.S01E02.1080p.WEB-DL-NTb.mkv"):  # fmt: skip
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"\0" * 2000)
    return root


def test_helpers() -> None:
    ours = {"2160p": "a", "1080p": "b", "720p": "c", "480p": "d", "Any": "e"}
    assert [guess_profile(n, ours) for n in ("Ultra-HD", "HD-1080p", "HD-720p", "SD", "Any",
            "HD - 720p/1080p", "Weird")] == ["a", "b", "c", "d", "e", "b", "b"]  # fmt: skip
    maps = [{"from": "/data/media", "to": "/mnt/nas/media"}]
    assert translate("/data/media/movies/X", maps) == "/mnt/nas/media/movies/X"
    assert translate("/other/X", maps) == "/other/X"


async def test_move_from_radarr_and_sonarr(
    c: httpx.AsyncClient, fakes: Fakes, server: Running, media: Path
) -> None:
    server.app.state.migrator.transport = fake_arr(media)
    # without a TMDB key: refused up front, not every title skipped at the end
    await c.patch("/system/config", json={"tmdb": {"api_key": None}})
    r = await c.post("/library/migrate/scan", json={"kind": "radarr",
                     "url": "http://radarr:7878", "api_key": KEY})  # fmt: skip
    assert r.status_code == 409 and "TMDB key" in r.text
    from tests.fake_tmdb import V3_KEY

    await c.patch("/system/config", json={"tmdb": {"api_key": V3_KEY}})
    r = await c.post("/library/migrate/scan", json={"kind": "radarr",
                     "url": "http://radarr:7878", "api_key": "wrong"})  # fmt: skip
    assert r.status_code == 422 and "refused the API key" in r.text
    r = await c.post("/library/migrate/scan", json={"kind": "sonarr",
                     "url": "http://radarr:7878", "api_key": KEY})  # fmt: skip
    assert r.status_code == 422 and "not Sonarr" in r.text  # the wrong app

    radarr = {"kind": "radarr", "url": "http://radarr:7878", "api_key": KEY}
    scan = (await _job(c, (await c.post("/library/migrate/scan", json=radarr)).json()["id"]))[
        "result"
    ]
    assert scan["counts"] == {"titles": 2, "with_files": 1, "missing_tmdb": 0}
    assert scan["indexers"] == [{"name": "Local Prowlarr", "implementation": "Torznab",
                                 "url": "http://127.0.0.1:9697/1"}]  # fmt: skip
    assert "fixture" not in json.dumps(scan) and KEY not in json.dumps(scan)
    res = (await _job(c, (await c.post("/library/migrate/apply", json=radarr)).json()["id"]))[
        "result"
    ]
    assert res["added"] == 2 and res["files"] == 1 and res["skipped"] == []

    sonarr = {"kind": "sonarr", "url": "http://sonarr:8989", "api_key": KEY}
    res = (await _job(c, (await c.post("/library/migrate/apply", json=sonarr)).json()["id"]))[
        "result"
    ]
    assert res["added"] == 1 and res["files"] == 2

    titles = {t["name"]: t for t in (await c.get("/titles")).json()["items"]}
    dune, amelie = titles["Dune"]["wanted"][0], titles["Amélie"]["wanted"][0]
    assert dune["state"] == "acquired" and amelie["state"] == "missing" and amelie["monitored"]
    assert amelie["next_search_at"] is not None  # no search storm right away
    [f] = (await c.get(f"/titles/{titles['Dune']['id']}/files")).json()
    assert f["relative_path"] == "Dune (2021)/Dune.2021.1080p.BluRay.x264-GRP.mkv"
    assert f["link_mode"] == "existing"  # recorded in place, nothing moved
    profiles = {p["id"]: p["name"] for p in (await c.get("/quality-profiles")).json()}
    assert profiles[titles["Dune"]["profile_id"]] == "1080p"  # their HD-1080p
    seasons = (await c.get(f"/titles/{titles['Severance']['id']}/episodes")).json()
    got = {(s["number"], e["number"]) for s in seasons for e in s["episodes"]
           if e["wanted"]["state"] == "acquired"}  # fmt: skip
    assert got == {(1, 1), (1, 2)}
    folders = {d["name"]: d["path"] for d in (await c.get("/destinations")).json()}
    assert folders["Radarr movies"] == str(media / "movies")

    # running it again changes nothing
    res = (await _job(c, (await c.post("/library/migrate/apply", json=radarr)).json()["id"]))[
        "result"
    ]
    assert res["added"] == 0 and res["files"] == 0
