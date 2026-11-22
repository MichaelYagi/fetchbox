"""Quality profiles and custom formats through the API."""

from __future__ import annotations

from typing import Any

import httpx

from tests.conftest import Running

LADDER = [
    {"name": "WEB 1080p", "qualities": ["WEBDL-1080p", "WEBRip-1080p"]},
    {"name": "Bluray-1080p", "qualities": ["Bluray-1080p"]},
]


async def _create_profile(c: httpx.AsyncClient, **kw: Any) -> dict[str, Any]:
    body = {"name": "Mine", "ladder": LADDER, "cutoff": "WEB 1080p", **kw}
    r = await c.post("/quality-profiles", json=body)
    assert r.status_code == 201, r.text
    assert r.headers["etag"] == '"v1"'
    assert r.headers["location"].endswith(r.json()["id"])
    out: dict[str, Any] = r.json()
    return out


async def test_defaults_are_seeded_once(server: Running, settings: Any) -> None:
    async with server.client() as c:
        profiles = (await c.get("/quality-profiles")).json()
        formats = (await c.get("/custom-formats")).json()
    video = [p["name"] for p in profiles if p["media"] == "video"]
    assert video == ["1080p", "2160p", "480p", "720p", "Any"]
    music = [p["name"] for p in profiles if p["media"] == "music"]
    assert music == ["Any audio", "High-quality lossy", "Lossless"]
    hd = next(p for p in profiles if p["name"] == "1080p")
    assert hd["cutoff"] == "WEB 1080p" and hd["languages"] == ["original"]
    assert {f["name"] for f in formats} >= {"Hardcoded subtitles", "3D", "Dolby Vision"}
    four_k = next(p for p in profiles if p["name"] == "2160p")
    dv = next(f for f in formats if f["name"] == "Dolby Vision")
    assert four_k["format_scores"][dv["id"]] == 20


async def test_seed_does_not_overwrite_edits(settings: Any) -> None:
    from asgi_lifespan import LifespanManager

    from fetchbox.app import create_app
    from tests.conftest import running

    async with running(settings) as srv, srv.client() as c:
        key = (await c.post("/auth/keys", json={"name": "t", "scopes": ["*"]})).json()["key"]
        hd = next(p for p in (await c.get("/quality-profiles")).json() if p["name"] == "1080p")
        r = await c.patch(f"/quality-profiles/{hd['id']}", json={"cutoff": "Bluray-1080p"})
        assert r.status_code == 200
        four_k = next(p for p in (await c.get("/quality-profiles")).json() if p["name"] == "2160p")
        assert (await c.delete(f"/quality-profiles/{four_k['id']}")).status_code == 204

    app = create_app(settings, on_bootstrap_key=lambda _k: None)  # restart: no new key
    async with (
        LifespanManager(app, startup_timeout=60, shutdown_timeout=60),
        Running(app, key).client() as c,
    ):
        got = (await c.get("/quality-profiles", params={"media": "video"})).json()
        names = {p["name"]: p for p in got}
    assert set(names) == {"1080p", "480p", "720p", "Any"}  # a deleted default stays deleted
    assert names["1080p"]["cutoff"] == "Bluray-1080p"


async def test_qualities_catalog(client: httpx.AsyncClient) -> None:
    q = (await client.get("/qualities")).json()
    names = [x["name"] for x in q]
    assert names.index("WEBDL-1080p") < names.index("Bluray-1080p") < names.index("Remux-2160p")
    web = next(x for x in q if x["name"] == "WEBDL-1080p")
    assert web["resolution"] == 1080 and web["default_size"]["min"] > 0


async def test_profile_crud_and_etag(client: httpx.AsyncClient) -> None:
    p = await _create_profile(client)
    assert p["upgrade_allowed"] is True and p["languages"] == ["original"]
    r = await client.get(f"/quality-profiles/{p['id']}")
    assert r.headers["etag"] == '"v1"' and r.json()["etag"] == '"v1"'

    r = await client.patch(
        f"/quality-profiles/{p['id']}",
        json={"cutoff": "Bluray-1080p", "min_format_score": 5},
        headers={"If-Match": '"v1"'},
    )
    assert r.status_code == 200 and r.headers["etag"] == '"v2"'
    assert r.json()["cutoff"] == "Bluray-1080p" and r.json()["ladder"] == LADDER

    stale = await client.patch(
        f"/quality-profiles/{p['id']}", json={"name": "x"}, headers={"If-Match": '"v1"'}
    )
    assert stale.status_code == 412

    r = await client.delete(f"/quality-profiles/{p['id']}", headers={"If-Match": '"v2"'})
    assert r.status_code == 204
    assert (await client.get(f"/quality-profiles/{p['id']}")).status_code == 404


async def test_profile_validation_errors(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/quality-profiles", json={"name": "Bad", "ladder": LADDER, "cutoff": "Nope"}
    )
    assert r.status_code == 422
    p = await _create_profile(client)
    # A PATCH that breaks the whole (cutoff no longer in the ladder) is rejected.
    r = await client.patch(f"/quality-profiles/{p['id']}", json={"ladder": [LADDER[1]]})
    assert r.status_code == 422
    assert any("cutoff" in e["message"] for e in r.json()["errors"])
    r = await client.patch(f"/quality-profiles/{p['id']}", json={"format_scores": {"nope": 5}})
    assert r.status_code == 422
    assert r.json()["errors"][0]["field"] == "body.format_scores.nope"
    assert (await client.get(f"/quality-profiles/{p['id']}")).json()["etag"] == '"v1"'


async def test_duplicate_names_conflict(client: httpx.AsyncClient) -> None:
    await _create_profile(client)
    r = await client.post(
        "/quality-profiles", json={"name": "Mine", "ladder": LADDER, "cutoff": "WEB 1080p"}
    )
    assert r.status_code == 409
    r = await client.post(
        "/custom-formats",
        json={"name": "3D", "conditions": [{"type": "flag", "values": ["three_d"]}]},
    )
    assert r.status_code == 409


async def test_custom_format_crud_and_delete_cleans_profiles(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/custom-formats",
        json={
            "name": "Preferred group",
            "default_score": 50,
            "conditions": [{"type": "release_group", "pattern": "^(FLUX|NTb)$"}],
        },
    )
    assert r.status_code == 201, r.text
    f = r.json()
    assert f["conditions"][0]["negate"] is False and r.headers["etag"] == '"v1"'

    r = await client.patch(
        f"/custom-formats/{f['id']}", json={"default_score": 60}, headers={"If-Match": '"v1"'}
    )
    assert r.status_code == 200 and r.json()["default_score"] == 60

    bad = await client.patch(
        f"/custom-formats/{f['id']}", json={"conditions": [{"type": "hdr", "values": ["X"]}]}
    )
    assert bad.status_code == 422

    p = await _create_profile(client, format_scores={f["id"]: 100})
    assert (await client.delete(f"/custom-formats/{f['id']}")).status_code == 204
    after = (await client.get(f"/quality-profiles/{p['id']}")).json()
    assert after["format_scores"] == {}
    assert after["etag"] == '"v2"'  # the cleanup is a real (versioned) change


async def test_evaluate(client: httpx.AsyncClient) -> None:
    profiles = {p["name"]: p for p in (await client.get("/quality-profiles")).json()}
    r = await client.post(
        f"/quality-profiles/{profiles['2160p']['id']}/evaluate",
        json={
            "original_language": "en",
            "runtime_minutes": 120,
            "releases": [
                {"title": "Dune.2021.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.HEVC-FLUX", "size": 18e9},
                {"title": "Dune.2021.1080p.BluRay.x264-SPARKS", "size": 100_000_000},
                {"title": "Dune.2021.CAM.x264-NOGRP"},
                {"title": "Dune.2021.FRENCH.2160p.WEB-DL.HC.HEVC-GRP"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    dv, bluray, cam, french = r.json()
    assert dv["allowed"] and dv["tier"] == "WEB 2160p" and dv["cutoff_met"]
    assert {m["name"] for m in dv["matched_formats"]} == {"Dolby Vision", "Atmos"}
    assert dv["format_score"] == 25 and dv["size_ok"] is True and dv["language_ok"]
    assert not bluray["allowed"] and bluray["tier"] is None  # 2160p takes 2160p only
    assert not cam["allowed"] and cam["tier"] is None and cam["size_ok"] is None
    assert not french["language_ok"] and not french["meets_min_score"]
    assert dv["parsed"]["group"] == "FLUX"


async def test_scopes(server: Running) -> None:
    async with server.client() as root:
        r = await root.post("/auth/keys", json={"name": "ro", "scopes": ["profiles:read"]})
        ro = r.json()["key"]
    async with server.client(ro) as c:
        assert (await c.get("/quality-profiles")).status_code == 200
        pid = (await c.get("/quality-profiles")).json()[0]["id"]
        ev = await c.post(f"/quality-profiles/{pid}/evaluate", json={"releases": [{"title": "x"}]})
        assert ev.status_code == 200
        r = await c.post(
            "/quality-profiles", json={"name": "n", "ladder": LADDER, "cutoff": "WEB 1080p"}
        )
        assert r.status_code == 403
        assert (await c.delete(f"/quality-profiles/{pid}")).status_code == 403


async def test_old_defaults_get_the_new_ones_once(server: Running) -> None:
    """A database from before the resolution-named defaults (Standard HD, 4K, Any) gets
    1080p / 2160p / 720p / 480p added next to its own, once."""
    from sqlalchemy import delete, update

    from fetchbox.modules.profiles.models import QualityProfile

    db = server.app.state.fb.db
    async with db.sessionmaker() as s:
        await s.execute(update(QualityProfile).where(QualityProfile.name == "1080p")
                        .values(name="Standard HD"))  # fmt: skip
        await s.execute(update(QualityProfile).where(QualityProfile.name == "2160p")
                        .values(name="4K"))  # fmt: skip
        await s.execute(delete(QualityProfile).where(QualityProfile.name.in_(["720p", "480p"])))
        await s.commit()
    svc = server.app.state.profiles
    await svc.seed_defaults()
    names = sorted(p.name for p in await svc.list_profiles() if p.media == "video")
    assert names == ["1080p", "2160p", "480p", "4K", "720p", "Any", "Standard HD"]
    # once: deleting a new one afterwards sticks
    async with db.sessionmaker() as s:
        await s.execute(delete(QualityProfile).where(QualityProfile.name == "480p"))
        await s.commit()
    await svc.seed_defaults()
    assert "480p" not in {p.name for p in await svc.list_profiles()}
