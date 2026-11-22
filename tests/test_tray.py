"""The desktop tray's supervisor (real server process) and POST /system/shutdown."""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import pytest

from fetchbox.config import load_settings
from fetchbox.tray import Supervisor, server_command
from tests.conftest import Running


async def test_tray_token_is_a_narrow_principal(
    server: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[bool] = []
    server.app.state.request_shutdown = lambda: stopped.append(True)
    monkeypatch.setenv("FETCHBOX_SUPERVISOR_TOKEN", "tray-secret")
    tray = {"X-Fetchbox-Supervisor": "tray-secret"}
    async with server.client(key="") as anon:
        assert (await anon.post("/system/shutdown")).status_code == 401
        r = await anon.post("/system/shutdown", headers={"X-Fetchbox-Supervisor": "wrong"})
        assert r.status_code == 401
        assert (await anon.post("/downloads/pause-all", headers=tray)).status_code == 200
        r = await anon.post("/titles", headers=tray, json={"tmdb_id": 1, "profile_id": "x"})
        assert r.status_code == 403  # only system:write and downloads:write
        # the sign-in link: one code, one session
        path = (await anon.post("/auth/login-link", headers=tray)).json()["path"]
        code = path.split("code=")[1]
        r = await anon.post("/auth/login-code", json={"code": code})
        assert r.status_code == 200 and "fb_session" in r.headers.get("set-cookie", "")
        assert (await anon.post("/auth/login-code", json={"code": code})).status_code == 401
        assert (await anon.post("/system/shutdown", headers=tray)).status_code == 202
    await asyncio.sleep(0.5)  # it stops just after answering
    assert stopped == [True]


def test_autostart_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from fetchbox import tray

    if sys.platform == "win32":
        pytest.skip("writes the real HKCU Run key")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert not tray.autostart_enabled()
    tray.set_autostart(True)
    assert tray.autostart_enabled()
    entry = tray._autostart_file().read_bytes()
    assert b"fetchbox" in entry.lower()
    tray.set_autostart(False)
    assert not tray.autostart_enabled()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_supervisor_starts_and_stops_a_real_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FETCHBOX_DOWNLOADS_ROOT", str(tmp_path / "Downloads"))
    monkeypatch.setenv("FETCHBOX_TORRENT_ISOLATED", "1")
    monkeypatch.setenv("FETCHBOX_SEED_INDEXERS", "0")
    settings = load_settings(tmp_path / "data", port=_free_port())
    cmd = server_command(settings)
    assert cmd[-4:] == ["--host", "0.0.0.0", "--port", str(settings.port)]
    sup = Supervisor(settings)
    sup.start()
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            sup.poll()
            if sup.healthy:
                break
            time.sleep(0.5)
        assert sup.healthy and sup.status == f"Running at {sup.url}"
        proc = sup.proc
        assert proc is not None
        sup.stop()  # graceful: through /system/shutdown with the token
        assert proc.returncode == 0, (tmp_path / "data/logs/server-console.log").read_text()
        assert sup.status == "Stopped" and not sup.ping()
        log = (tmp_path / "data/logs/server-console.log").read_text(errors="replace")
        assert "stopped" in log.lower() or "shutting down" in log.lower(), log[-2000:]
    finally:
        if sup.proc is not None:
            sup.proc.kill()


def test_icon_draws() -> None:
    pytest.importorskip("PIL")
    from fetchbox.tray import icon_image

    img = icon_image(64)
    assert img.size == (64, 64) and img.mode == "RGBA"
    assert img.getpixel((32, 4))[:3] == (37, 99, 235)
    assert icon_image(64, warning=True).getpixel((54, 10))[:3] == (217, 119, 6)


def test_access_from_other_devices_is_saved_in_config(tmp_path: Path) -> None:
    """The tray's "Allow access from other devices": on (the default) listens on every
    address; off keeps it to this computer, saved in config.toml, other lines kept."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.toml").write_text('port = 9999\n\n[network]\nhost = "keep-me"\n')
    sup = Supervisor(load_settings(data))
    assert sup.shared and sup.settings.host == "0.0.0.0"

    sup.set_shared(False)  # the server isn't running: nothing to restart
    assert not sup.shared
    assert load_settings(data).host == "127.0.0.1" and load_settings(data).port == 9999
    assert server_command(sup.settings)[-4:-2] == ["--host", "127.0.0.1"]
    text = (data / "config.toml").read_text()
    assert 'host = "keep-me"' in text  # a table's own key is left alone
    assert text.index('host = "127.0.0.1"') < text.index("[network]")

    sup.set_shared(True)
    assert sup.shared and load_settings(data).host == "0.0.0.0"
    assert 'host = "127.0.0.1"' not in (data / "config.toml").read_text()
