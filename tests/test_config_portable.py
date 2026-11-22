"""Config export / import: move a configured install to a fresh one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fetchbox.config import load_settings
from fetchbox.core.secrets import open_settings
from fetchbox.providers.indexers.torznab import TorznabSettings
from tests.conftest import running

TORZNAB_KEY = "torznab-secret-key-123"
TMDB_KEY = "0123456789abcdef0123456789abcdef"


async def _configure(srv: Any, lib: Path) -> None:
    async with srv.client() as c:
        assert (
            await c.patch("/system/config", json={"tmdb": {"api_key": TMDB_KEY}})
        ).status_code == 200
        ix = {"url": "http://idx.example", "api_key": TORZNAB_KEY}
        r = await c.post(
            "/indexers", json={"name": "My indexer", "kind": "torznab", "settings": ix}
        )
        assert r.status_code == 201, r.text
        hook = {"url": "http://hook.example/x", "secret": "s3cret-value"}
        r = await c.post(
            "/notifications/targets", json={"kind": "webhook", "name": "Hook", "settings": hook}
        )
        assert r.status_code == 201, r.text
        prof = (await c.get("/quality-profiles")).json()[0]
        r = await c.post("/quality-profiles", json={**{k: prof[k] for k in prof
                         if k not in ("id", "etag")}, "name": "Mine"})  # fmt: skip
        assert r.status_code == 201, r.text
        movies = next(
            d for d in (await c.get("/destinations")).json() if d["media_kind"] == "movie"
        )
        r = await c.patch(f"/destinations/{movies['id']}", json={"path": str(lib)},
                          headers={"If-Match": movies["etag"]})  # fmt: skip
        assert r.status_code == 200, r.text
        r = await c.patch("/automation/schedules/rss_sync", json={"interval_seconds": 1800})
        assert r.status_code == 200, r.text


async def test_move_configuration_to_a_fresh_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("FETCHBOX_DATA_DIR", "FETCHBOX_PORT", "FETCHBOX_DB_URL", "FETCHBOX_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FETCHBOX_TORRENT_ISOLATED", "1")
    monkeypatch.setenv("FETCHBOX_SEED_INDEXERS", "0")
    monkeypatch.setenv("FETCHBOX_DOWNLOADS_ROOT", str(tmp_path / "A-dl"))
    a = load_settings(tmp_path / "A")
    lib = tmp_path / "A-library"
    lib.mkdir()
    async with running(a) as srv:
        await _configure(srv, lib)
        async with srv.client() as c:
            plain = (await c.get("/config/export")).json()
            doc = (await c.post("/config/export", json={"password": "a good password"})).json()
    text = json.dumps(plain)
    assert TORZNAB_KEY not in text and TMDB_KEY not in text and "s3cret-value" not in text
    assert TORZNAB_KEY not in json.dumps(doc)  # encrypted, not readable
    assert {r["name"] for r in doc["tables"]["indexer"]} == {"My indexer"}

    monkeypatch.setenv("FETCHBOX_DOWNLOADS_ROOT", str(tmp_path / "B-dl"))
    b = load_settings(tmp_path / "B")
    async with running(b) as srv:
        stopped: list[bool] = []
        srv.app.state.request_shutdown = lambda: stopped.append(True)
        async with srv.client() as c:
            r = await c.post("/config/import", json={"document": doc})
            assert r.status_code == 422 and "password" in r.text
            r = await c.post("/config/import", json={"document": doc, "password": "wrong one!"})
            assert r.status_code == 422 and "wrong password" in r.text
            body = {"document": doc, "password": "a good password"}
            dry = (await c.post("/config/import", json=body)).json()
            assert not dry["applied"]
            plan = dry["plan"]
            assert plan["indexer"]["create"] == ["My indexer"]
            assert "Movies" in plan["destination"]["update"]  # B's own Movies, not a copy
            assert "Mine" in plan["quality_profile"]["create"]
            assert plan["schedule"]["update"] == ["rss_sync"]
            assert "tmdb" in "".join(plan["system"]["update"])
            assert (await c.get("/indexers")).json() == []  # a dry run changed nothing

            r = await c.post("/config/import", json={**body, "apply": True})
            assert r.status_code == 200 and r.json()["applied"] and r.json()["restarting"], r.text

            [ix] = (await c.get("/indexers")).json()
            assert ix["name"] == "My indexer"
            dests = (await c.get("/destinations")).json()
            movies = [d for d in dests if d["media_kind"] == "movie"]
            assert len(movies) == 1 and movies[0]["path"] == str(lib)
            names = {p["name"] for p in (await c.get("/quality-profiles")).json()}
            assert "Mine" in names
            sched = next(
                x for x in (await c.get("/automation/schedules")).json() if x["name"] == "rss_sync"
            )
            assert sched["interval_seconds"] == 1800

        # the secrets work with B's own key
        from sqlalchemy import select

        from fetchbox.modules.sources.models import Indexer

        async with srv.app.state.fb.db.sessionmaker() as s:
            row = (await s.execute(select(Indexer))).scalar_one()
        opened = open_settings(srv.app.state.fb.secrets, TorznabSettings, row.settings,
                               context=f"indexer:{row.id}")  # fmt: skip
        assert opened.api_key.get_secret_value() == TORZNAB_KEY

        # importing the plain export again: nothing new, secrets kept
        async with srv.client() as c:
            again = (await c.post("/config/import", json={"document": plain})).json()["plan"]
            assert again["indexer"]["create"] == [] and again["indexer"]["update"] == []
