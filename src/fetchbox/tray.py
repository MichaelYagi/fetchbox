"""Desktop tray launcher: supervises the server process and opens the web UI.

The server and the tray are separate processes (spec: "Tray launcher and deployment"):
the server runs headless exactly as everywhere else, and the tray only starts, watches
and stops it, so a server crash leaves the tray alive to say so. The tray stops the
server gracefully through ``POST /system/shutdown`` with a token it hands the server
when starting it (no API key or password needed, and it works on Windows, where
killing a process isn't graceful).

That server also writes ``server.json`` in the data folder (its pid, port, version and
token; readable by this user only), so a tray that didn't start it (the next version,
after an update) can tell what's answering on the port: the same version is used as is,
an older one is stopped and replaced.

Needs the ``desktop`` extra (pystray, Pillow). pystray is LGPL-3.0, used unmodified;
Fetchbox's source and build scripts are public, so a frozen build can be rebuilt with
a different pystray.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import httpx

import fetchbox
from fetchbox import updater
from fetchbox.config import Settings, write_config_value

POLL_SECONDS = 3.0
HANDOVER_FILE = "server.json"
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


def lan_address() -> str | None:
    """This computer's address on the local network (no packet is sent)."""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # a reserved address: only picks the outgoing interface
        ip = str(s.getsockname()[0])
        if not ip.startswith(("127.", "0.")):
            return ip
    return None


def write_handover(data_dir: Path, *, port: int, token: str) -> Path:
    """By the server a tray started: what a later tray needs to use or stop it."""
    path = data_dir / HANDOVER_FILE
    tmp = path.with_suffix(".tmp")
    info = {"pid": os.getpid(), "port": port, "version": fetchbox.__version__, "token": token}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(info, f)
    os.replace(tmp, path)
    return path


def clear_handover(data_dir: Path) -> None:
    """On the server's way out (only its own file: a newer server may have replaced it)."""
    path = data_dir / HANDOVER_FILE
    with contextlib.suppress(OSError, ValueError):
        if json.loads(path.read_text()).get("pid") == os.getpid():
            path.unlink()


def read_handover(data_dir: Path, port: int) -> dict[str, Any] | None:
    try:
        info = json.loads((data_dir / HANDOVER_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("port") != port or not info.get("token"):
        return None
    return info


class Supervisor:
    """Starts, watches and stops the server process (or one a tray before it started)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
        self.url = f"http://{host}:{settings.port}"
        self.token = secrets.token_urlsafe(32)
        self.proc: subprocess.Popen[bytes] | None = None
        self.status = "Starting…"
        self.healthy = False
        self.exit_code: int | None = None
        self.attached = False  # using a server this tray didn't start (see server.json)
        self.other_pid: int | None = None
        self._lock = threading.Lock()

    def ping(self) -> bool:
        try:
            r = httpx.get(f"{self.url}/api/v1/system/ping", timeout=2.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    @property
    def running(self) -> bool:
        return (self.proc is not None and self.proc.poll() is None) or self.attached

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            if self.ping() and not self._take_over():
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

    def _take_over(self) -> bool:
        """A server already answers here. This version's (a tray started it): use it.
        Another version's: stop it, so this one starts (True). Unknown (not started by a
        tray, or by one before server.json): use it, as before."""
        info = read_handover(self.settings.data_dir, self.settings.port)
        if info is None or info.get("version") == fetchbox.__version__:
            if info is not None:
                self.token, self.other_pid = str(info["token"]), info.get("pid")
            self.attached = True
            self.status, self.healthy = f"Running at {self.url}", True
            return False
        self.status = f"Stopping Fetchbox {info.get('version')}…"
        self._shut_down_other(str(info["token"]), info.get("pid"))
        if self.ping():
            self.status = (
                f"Fetchbox {info.get('version')} still answers at {self.url}: quit it, "
                "then Start server"
            )
            return False
        return True

    def _shut_down_other(self, token: str, pid: Any) -> None:
        """Gracefully through its token; if it won't go, its pid is told to stop."""
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{self.url}/api/v1/system/shutdown",
                       headers={"X-Fetchbox-Supervisor": token}, timeout=5.0)  # fmt: skip
        if self._gone_within(STOP_GRACE_SECONDS):
            return
        if isinstance(pid, int) and pid > 0:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
            self._gone_within(10.0)

    def _gone_within(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while self.ping():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.5)
        return True

    def stop(self) -> None:
        with self._lock:
            if self.attached and self.proc is None:  # one a tray before this started
                self.status = "Stopping…"
                self._shut_down_other(self.token, self.other_pid)
                self.attached, self.other_pid = False, None
                self.token = secrets.token_urlsafe(32)  # its token went with it
                self.status, self.healthy = "Stopped", False
                return
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

    def call(self, method: str, path: str) -> httpx.Response | None:
        """The API with the tray's own token (scopes system:write, downloads:write)."""
        try:
            return httpx.request(method, f"{self.url}/api/v1{path}", timeout=10.0,
                                 headers={"X-Fetchbox-Supervisor": self.token})  # fmt: skip
        except httpx.HTTPError:
            return None

    def open_url(self) -> str:
        """The UI, signed in: a one-time link (no password prompt on this computer)."""
        r = self.call("POST", "/auth/login-link")
        if r is not None and r.status_code == 200:
            return self.url + str(r.json()["path"])
        return self.url

    def restart(self) -> None:
        self.stop()
        self.start()

    # ---- access from other devices ------------------------------------------------------

    @property
    def shared(self) -> bool:
        return not self.settings.is_loopback

    def network_url(self) -> str | None:
        ip = lan_address() if self.shared else None
        return f"http://{ip}:{self.settings.port}" if ip else None

    def set_shared(self, on: bool) -> None:
        """Listen on every address (the default: other devices on the network) or only
        on this computer; saved in config.toml, applied by restarting the server."""
        if on == self.shared:
            return
        write_config_value(self.settings.data_dir, "host", None if on else "127.0.0.1")
        self.settings.host = "0.0.0.0" if on else "127.0.0.1"
        if self.running:
            self.restart()

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
            self.status == "Stopped" or self.status.startswith(("Server stopped", "Fetchbox "))
        ):
            return
        ok = self.ping()
        self.healthy = ok
        if ok:
            self.status = f"Running at {self.url}"
        elif self.attached:  # the server it was using went away
            self.attached, self.other_pid = False, None
            self.status = "Server stopped: Start server to run it again"
        elif self.running:
            self.status = "Starting…"


# ---- start at login (Windows Run key, macOS LaunchAgent, XDG autostart) ----------------------

APP_ID = "dev.fetchbox.Fetchbox"


def _launch_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]  # the desktop build starts the tray with no arguments
    return [sys.executable, "-m", "fetchbox", "tray"]


def autostart_enabled() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as k:  # fmt: skip
                winreg.QueryValueEx(k, "Fetchbox")
            return True
        except OSError:
            return False
    return _autostart_file().is_file()


def _autostart_file() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{APP_ID}.plist"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "fetchbox.desktop"


def set_autostart(on: bool) -> None:
    cmd = _launch_command()
    if sys.platform == "win32":
        import subprocess as sp
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "Fetchbox", 0, winreg.REG_SZ, sp.list2cmdline(cmd))
            else:
                with contextlib.suppress(OSError):
                    winreg.DeleteValue(k, "Fetchbox")
        return
    f = _autostart_file()
    if not on:
        f.unlink(missing_ok=True)
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        import plistlib

        f.write_bytes(plistlib.dumps({"Label": APP_ID, "ProgramArguments": cmd,
                                      "RunAtLoad": True}))  # fmt: skip
    else:
        import shlex

        f.write_text("[Desktop Entry]\nType=Application\nName=Fetchbox\n"
                     f"Exec={shlex.join(cmd)}\nX-GNOME-Autostart-enabled=true\n")  # fmt: skip


# ---- updates (an installed Windows / macOS copy) -----------------------------------------

UPDATE_EVERY = 6 * 3600.0


class Updates:
    """Asks the server (which honours Settings > "Check for updates") what's released."""

    def __init__(self, sup: Supervisor, data_dir: Path) -> None:
        self.sup = sup
        self.folder = data_dir / "updates"
        self.latest: str | None = None
        self.page: str | None = None
        self.files: tuple[Any, Any] | None = None  # (installer, checksums) for this computer
        self.busy = ""
        self.error = ""
        self._checked = 0.0

    @property
    def label(self) -> str:
        if self.busy:
            return self.busy
        if self.error:
            return f"Update failed ({self.error[:60]}): try again"
        verb = "Install" if self.files and updater.installed_app() else "Download"
        return f"{verb} Fetchbox {self.latest}"

    def maybe_check(self, *, force: bool = False) -> None:
        if not self.sup.healthy:
            return
        if not force and time.monotonic() - self._checked < UPDATE_EVERY:
            return
        self._checked = time.monotonic()
        r = self.sup.call("GET", "/system/updates" + ("?fresh=true" if force else ""))
        if r is None or r.status_code != 200:
            return
        info = r.json()
        if not info.get("update_available"):
            self.latest = None
            return
        self.latest, self.page = info["latest"], info.get("url")
        found = [updater.Download(d["name"], d["url"], d.get("size")) for d in info["downloads"]]
        self.files = updater.pick(found)

    def asked(self) -> bool:
        """The web UI's "Install" (Settings): asked of the server, taken here once."""
        if not self.sup.healthy or self.busy:
            return False
        r = self.sup.call("POST", "/system/tray/requests")
        if r is None or r.status_code != 200 or not r.json().get("install"):
            return False
        self.maybe_check(force=True)  # the installer for this computer, fresh
        return bool(self.files)

    def install(self, quit_tray: Any) -> None:
        app = updater.installed_app()
        if not (self.files and app):
            if self.page:
                webbrowser.open(self.page)
            return
        try:
            self.error = ""
            self.busy = f"Downloading Fetchbox {self.latest}…"
            path = updater.download(*self.files, self.folder)
            self.busy = f"Installing Fetchbox {self.latest}…"
            self.sup.stop()  # jobs finish, torrents save their state
            updater.apply(path, app, wait_pid=os.getpid())
        except (updater.UpdateError, OSError) as exc:
            self.busy, self.error = "", str(exc)
            if not self.sup.running:
                self.sup.start()
            return
        quit_tray()


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
        webbrowser.open(sup.open_url())

    def quit_(icon: Any, *_: Any) -> None:
        stopping.set()
        sup.stop()
        icon.stop()

    updates = Updates(sup, settings.data_dir)

    def toggle_shared(*_: Any) -> None:
        def work() -> None:
            sup.set_shared(not sup.shared)

        threading.Thread(target=work, daemon=True, name="tray-share").start()

    def install_update(icon: Any, *_: Any) -> None:
        def quit_tray() -> None:
            stopping.set()
            icon.stop()

        threading.Thread(target=updates.install, args=(quit_tray,), daemon=True,
                         name="tray-update").start()  # fmt: skip

    menu = pystray.Menu(
        pystray.MenuItem(f"Fetchbox {fetchbox.__version__}", None, enabled=False),
        pystray.MenuItem(lambda _: sup.status, None, enabled=False),
        pystray.MenuItem(
            lambda _: updates.label,
            install_update,
            visible=lambda _: updates.latest is not None,
            enabled=lambda _: not updates.busy,
        ),
        pystray.MenuItem("Open Fetchbox", open_ui, default=True, enabled=lambda _: sup.healthy),
        pystray.MenuItem(
            "Pause all downloads",
            lambda *_: sup.call("POST", "/downloads/pause-all"),
            enabled=lambda _: sup.healthy,
        ),
        pystray.MenuItem(
            "Resume all downloads",
            lambda *_: sup.call("POST", "/downloads/resume-all"),
            enabled=lambda _: sup.healthy,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start server", lambda *_: sup.start(), visible=lambda _: not sup.running),
        pystray.MenuItem("Restart server", lambda *_: sup.restart(), visible=lambda _: sup.running),
        pystray.MenuItem("Stop server", lambda *_: sup.stop(), visible=lambda _: sup.running),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Allow access from other devices", toggle_shared, checked=lambda _: sup.shared
        ),
        pystray.MenuItem(
            lambda _: f"Other devices: {sup.network_url()}",
            None,
            enabled=False,
            visible=lambda _: sup.network_url() is not None,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open data folder", lambda *_: _open_folder(settings.data_dir)),
        pystray.MenuItem("Open logs folder", lambda *_: _open_folder(settings.data_dir / "logs")),
        pystray.MenuItem(
            "Start at login",
            lambda *_: set_autostart(not autostart_enabled()),
            checked=lambda _: autostart_enabled(),
        ),
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
            updates.maybe_check()
            if updates.asked():
                install_update(icon)
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
