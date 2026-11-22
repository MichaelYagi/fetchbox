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


async def test_shutdown_needs_system_write_or_the_supervisor_token(
    server: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[bool] = []
    server.app.state.request_shutdown = lambda: stopped.append(True)
    async with server.client(key="") as anon:
        assert (await anon.post("/system/shutdown")).status_code == 401
        monkeypatch.setenv("FETCHBOX_SUPERVISOR_TOKEN", "tray-secret")
        r = await anon.post("/system/shutdown", headers={"X-Fetchbox-Supervisor": "wrong"})
        assert r.status_code == 401
        r = await anon.post("/system/shutdown", headers={"X-Fetchbox-Supervisor": "tray-secret"})
        assert r.status_code == 202
    async with server.client() as root:
        assert (await root.post("/system/shutdown")).status_code == 202
    await asyncio.sleep(0.5)  # it stops just after answering
    assert stopped == [True, True]


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
    assert cmd[-4:] == ["--host", "127.0.0.1", "--port", str(settings.port)]
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
