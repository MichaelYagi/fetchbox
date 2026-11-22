"""Desktop tray launcher: supervises the server process and opens the web UI.

The server and the tray are separate processes (spec: "Tray launcher and deployment"):
the server runs headless exactly as everywhere else, and the tray only starts, watches
and stops it, so a server crash leaves the tray alive to say so. The tray stops the
server gracefully through ``POST /system/shutdown`` with a token it hands the server
when starting it (no API key or password needed, and it works on Windows, where
killing a process isn't graceful).

Needs the ``desktop`` extra (pystray, Pillow). pystray is LGPL-3.0, used unmodified;
Fetchbox's source and build scripts are public, so a frozen build can be rebuilt with
a different pystray.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import httpx

from fetchbox.config import Settings

POLL_SECONDS = 3.0
STOP_GRACE_SECONDS = 40.0
ACCENT = (37, 99, 235)  # the web UI's --accent


def icon_image(size: int = 64, *, warning: bool = False) -> Any:
    """The Fetchbox mark (a box on a blue rounded square), drawn: no image files."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size // 5
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=r, fill=ACCENT)
    s = size / 64
    w = max(2, round(4 * s))
    top = [(32 * s, 14 * s), (50 * s, 23 * s), (32 * s, 32 * s), (14 * s, 23 * s)]
    d.polygon(top, outline="white", width=w)
    d.line([(14 * s, 23 * s), (14 * s, 41 * s), (32 * s, 50 * s), (50 * s, 41 * s),
            (50 * s, 23 * s)], fill="white", width=w, joint="curve")  # fmt: skip
    d.line([(32 * s, 32 * s), (32 * s, 50 * s)], fill="white", width=w)
    if warning:
        d.ellipse((size * 0.62, 0, size - 1, size * 0.38), fill=(217, 119, 6))
    return img


def server_command(settings: Settings) -> list[str]:
    """This same program, as ``fetchbox serve`` (frozen builds re-invoke themselves)."""
    base = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "fetchbox"]
    return [*base, "--data", str(settings.data_dir), "serve",
            "--host", settings.host, "--port", str(settings.port)]  # fmt: skip


def _open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class Supervisor:
    """Starts, watches and stops the server process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
        self.url = f"http://{host}:{settings.port}"
        self.token = secrets.token_urlsafe(32)
        self.proc: subprocess.Popen[bytes] | None = None
        self.status = "Starting…"
        self.healthy = False
        self.exit_code: int | None = None
        self._lock = threading.Lock()

    def ping(self) -> bool:
        try:
            r = httpx.get(f"{self.url}/api/v1/system/ping", timeout=2.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            if self.ping():  # someone else's server already answers here: use it
                self.status, self.healthy = f"Running at {self.url}", True
                return
            logs = self.settings.data_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            out = open(logs / "server-console.log", "ab")  # noqa: SIM115 - the child owns it
            flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            self.proc = subprocess.Popen(
                server_command(self.settings),
                stdout=out,
                stderr=subprocess.STDOUT,
                env={**os.environ, "FETCHBOX_SUPERVISOR_TOKEN": self.token},
                creationflags=flags,
            )
            self.exit_code = None
            self.status, self.healthy = "Starting…", False

    def stop(self) -> None:
        with self._lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return
            self.status = "Stopping…"
            try:
                httpx.post(
                    f"{self.url}/api/v1/system/shutdown",
                    headers={"X-Fetchbox-Supervisor": self.token},
                    timeout=5.0,
                )
                proc.wait(STOP_GRACE_SECONDS)
            except (httpx.HTTPError, subprocess.TimeoutExpired):
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self.proc = None
            self.status, self.healthy = "Stopped", False

    def restart(self) -> None:
        self.stop()
        self.start()

    def poll(self) -> None:
        """Refresh the status line (called every few seconds)."""
        if self.proc is not None and self.proc.poll() is not None:
            self.exit_code = self.proc.returncode
            self.proc = None
            self.status, self.healthy = (
                f"Server stopped unexpectedly (exit {self.exit_code}): see the logs folder",
                False,
            )
            return
        if self.proc is None and (
            self.status == "Stopped" or self.status.startswith("Server stopped")
        ):
            return
        ok = self.ping()
        self.healthy = ok
        if ok:
            self.status = f"Running at {self.url}"
        elif self.running:
            self.status = "Starting…"


def run(settings: Settings) -> int:
    try:
        import pystray
    except ImportError:
        print("the tray needs the desktop extra: pip install 'fetchbox[desktop]'", file=sys.stderr)
        return 2

    sup = Supervisor(settings)
    sup.start()
    stopping = threading.Event()

    def open_ui(*_: Any) -> None:
        webbrowser.open(sup.url)

    def quit_(icon: Any, *_: Any) -> None:
        stopping.set()
        sup.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: sup.status, None, enabled=False),
        pystray.MenuItem("Open Fetchbox", open_ui, default=True, enabled=lambda _: sup.healthy),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start server", lambda *_: sup.start(), visible=lambda _: not sup.running),
        pystray.MenuItem("Restart server", lambda *_: sup.restart(), visible=lambda _: sup.running),
        pystray.MenuItem("Stop server", lambda *_: sup.stop(), visible=lambda _: sup.running),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open data folder", lambda *_: _open_folder(settings.data_dir)),
        pystray.MenuItem("Open logs folder", lambda *_: _open_folder(settings.data_dir / "logs")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Fetchbox", quit_),
    )
    icon = pystray.Icon("fetchbox", icon_image(), "Fetchbox", menu)
    opened = False

    def watch() -> None:
        nonlocal opened
        warned = False
        while not stopping.is_set():
            sup.poll()
            if sup.healthy and not opened:  # first start: open the UI once it answers
                opened = True
                open_ui()
            bad = not sup.healthy and not sup.status.startswith(("Starting", "Stopping"))
            if bad != warned:
                warned = bad
                icon.icon = icon_image(warning=bad)
            icon.title = f"Fetchbox: {sup.status}"
            icon.update_menu()
            time.sleep(POLL_SECONDS)

    threading.Thread(target=watch, daemon=True, name="tray-watch").start()
    icon.run()
    return 0
