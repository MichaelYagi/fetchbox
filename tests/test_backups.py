"""Backups and restore: made safely while running, restored at the next start."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from asgi_lifespan import LifespanManager

from fetchbox import migrations
from fetchbox.app import create_app
from fetchbox.config import Settings
from fetchbox.modules.system.backups import BackupError, Backups, unwrap_key, wrap_key
from tests.conftest import Running, running
from tests.fake_tmdb import V3_KEY
from tests.pipeline import Fakes, _movie


def test_key_wrapping() -> None:
    blob = wrap_key(b"secret-key-bytes", "a long password")
    assert unwrap_key(blob, "a long password") == b"secret-key-bytes"
    with pytest.raises(BackupError, match="wrong password"):
        unwrap_key(blob, "not it at all")


async def test_backup_download_upload_and_restore(settings: Settings) -> None:
    stopped: list[bool] = []
    async with running(settings) as srv:
        srv.app.state.request_shutdown = lambda: stopped.append(True)
        fakes = Fakes()
        srv.app.state.discovery.transport = fakes.tmdb.transport
        root_key = srv.root_key
        async with srv.client() as c:
            await c.patch("/system/config", json={"tmdb": {"api_key": V3_KEY}})
            movie = await _movie(c)
            r = await c.post("/backups", json={"password": "correct horse 42"})
            assert r.status_code == 201, r.text
            b = r.json()
            assert b["reason"] == "manual" and b["includes_secrets_key"]
            assert b["schema_revision"] == migrations.head_revision()

            data = (await c.get(f"/backups/{b['id']}")).content
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                assert {"fetchbox.db", "manifest.json", "secrets.key.enc"} <= set(z.namelist())

            # after the backup: the movie is removed
            assert (await c.delete(f"/titles/{movie['id']}")).status_code == 204

            # a backup from a newer Fetchbox is refused on upload
            bad = io.BytesIO()
            with zipfile.ZipFile(bad, "w") as z:
                z.writestr("fetchbox.db", b"x")
                z.writestr("manifest.json", json.dumps({"created_at": "2030-01-01T00:00:00+00:00",
                           "schema_revision": "9999_future", "app_version": "9.0"}))  # fmt: skip
            r = await c.post("/backups/upload", files={"file": ("new.zip", bad.getvalue())})
            assert r.status_code == 422 and "newer Fetchbox" in r.text
            r = await c.post("/backups/upload", files={"file": ("mine.zip", data)})
            assert r.status_code == 201 and r.json()["reason"] == "manual"

            r = await c.post(f"/backups/{b['id']}/restore", json={})
            assert r.status_code == 422 and "password" in r.text
            r = await c.post(f"/backups/{b['id']}/restore", json={"password": "correct horse 42"})
            assert r.status_code == 202 and r.json()["restarting"], r.text
        import asyncio

        await asyncio.sleep(0.8)
        assert stopped == [True]

    # the next start applies it: the movie is back, the previous data kept aside
    app = create_app(settings, on_bootstrap_key=lambda k: None)
    async with (
        LifespanManager(app, startup_timeout=60, shutdown_timeout=60),
        Running(app, root_key).client() as c,
    ):
        titles = (await c.get("/titles")).json()["items"]
        assert [t["id"] for t in titles] == [movie["id"]]
        reasons = sorted(x["reason"] for x in (await c.get("/backups")).json())
        assert reasons == ["before-restore", "manual", "manual"]
        assert (await c.delete(f"/backups/{b['id']}")).status_code == 204
        assert (await c.get(f"/backups/{b['id']}")).status_code == 404


def test_pre_migration_backup(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrations.upgrade(settings.database_url, "0016_media_server")  # an older install
    svc = Backups(settings.data_dir, settings.database_url)
    b = svc.before_migrations()
    assert b is not None and b.reason == "pre-migration"
    assert b.schema_revision == "0016_media_server"
    migrations.upgrade(settings.database_url)
    assert svc.before_migrations() is None  # up to date: nothing to do


def test_cli_backup_and_restore(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from fetchbox import cli

    data = str(settings.data_dir)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrations.upgrade(settings.database_url)
    assert cli.main(["--data", data, "backup"]) == 0
    [b] = Backups(settings.data_dir, settings.database_url).list()
    assert cli.main(["--data", data, "restore", str(b.path)]) == 0
    reasons = sorted(x.reason for x in Backups(settings.data_dir, settings.database_url).list())
    assert reasons == ["before-restore", "manual"]
