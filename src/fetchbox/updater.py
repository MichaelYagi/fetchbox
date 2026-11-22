"""Install a newer Fetchbox from the desktop tray (Windows installer, macOS disk image).

The release carries ``SHA256SUMS-desktop.txt``; a download that doesn't match it is
refused. Windows: the installer runs silently over the installed copy (keeping the data
and choices) and starts Fetchbox again (``/RELAUNCH=1``). macOS: a small script waits
for Fetchbox to quit, replaces the .app from the disk image and opens it again.

Only for an installed copy that can be replaced: the Windows one the installer put in
place (``unins000.exe`` beside it), or a macOS .app in a folder this user can write to.
Anything else (the portable zip, Linux, Docker) gets the release page instead.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

CHECKSUMS = "SHA256SUMS-desktop.txt"


class UpdateError(Exception):
    pass


@dataclass
class Download:
    name: str
    url: str
    size: int | None = None


def pick(
    downloads: list[Download], *, system: str | None = None, machine: str | None = None
) -> tuple[Download, Download] | None:
    """(the installer for this computer, the checksum list), or None."""
    system = system or sys.platform
    machine = (machine or platform.machine()).lower()
    if system == "win32":
        part, ext = "Windows", "-Setup.exe"
    elif system == "darwin":
        part = "macOS-Silicon" if machine in ("arm64", "aarch64") else "macOS-Intel"
        ext = ".dmg"
    else:
        return None
    installer = next((d for d in downloads if part in d.name and d.name.endswith(ext)), None)
    sums = next((d for d in downloads if d.name == CHECKSUMS), None)
    return (installer, sums) if installer and sums else None


def installed_app() -> Path | None:
    """What an update would replace, when this copy can be updated in place."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable)
    if sys.platform == "win32":
        return exe if (exe.parent / "unins000.exe").is_file() else None
    if sys.platform == "darwin":
        app = exe.parents[2]  # Fetchbox.app/Contents/MacOS/Fetchbox
        if app.suffix == ".app" and os.access(app.parent, os.W_OK):
            return app
    return None


def expected_sha256(sums_text: str, name: str) -> str | None:
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    return None


def download(installer: Download, sums: Download, folder: Path, *, transport: Any = None) -> Path:
    """Fetch the installer into ``folder`` and check it against the release's sums."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / installer.name
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True,
                      transport=transport, headers={"User-Agent": "Fetchbox"}) as http:  # fmt: skip
        try:
            want = expected_sha256(http.get(sums.url).raise_for_status().text, installer.name)
            if want is None:
                raise UpdateError(f"{installer.name} isn't in the release's checksum list")
            digest = hashlib.sha256()
            part = target.with_suffix(target.suffix + ".part")
            with http.stream("GET", installer.url) as r, part.open("wb") as f:
                r.raise_for_status()
                for chunk in r.iter_bytes(1 << 20):
                    digest.update(chunk)
                    f.write(chunk)
        except httpx.HTTPError as exc:
            raise UpdateError(f"the download failed: {type(exc).__name__}") from None
    if digest.hexdigest() != want:
        part.unlink(missing_ok=True)
        raise UpdateError("the download doesn't match the release's checksum: not installed")
    os.replace(part, target)
    return target


def apply(installer: Path, app: Path, *, wait_pid: int, relaunch: bool = True) -> None:
    """Start installing; the caller (the tray) quits right after."""
    if sys.platform == "win32":
        args = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                "/FORCECLOSEAPPLICATIONS"]  # fmt: skip
        if relaunch:
            args.append("/RELAUNCH=1")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        subprocess.Popen(args, creationflags=flags, close_fds=True)
        return
    subprocess.Popen(
        ["/bin/sh", str(macos_script(installer, app, wait_pid=wait_pid, relaunch=relaunch))],
        start_new_session=True, close_fds=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip


def macos_script(dmg: Path, app: Path, *, wait_pid: int, relaunch: bool) -> Path:
    """Wait for Fetchbox to quit, swap the .app for the disk image's, open it again.
    The new copy goes in beside the old one first, so a failure leaves the old app."""
    q = shlex.quote
    new, old = q(str(app) + ".new"), q(str(app))
    text = f"""#!/bin/sh
while kill -0 {wait_pid} 2>/dev/null; do sleep 0.5; done
M="$(mktemp -d)"
hdiutil attach -nobrowse -readonly -mountpoint "$M" {q(str(dmg))} >/dev/null || exit 1
rm -rf {new}
if ditto "$M/Fetchbox.app" {new}; then
    rm -rf {old} && mv {new} {old}
fi
hdiutil detach "$M" >/dev/null 2>&1
rm -f {q(str(dmg))}
{"open " + old if relaunch else ""}
"""
    fd, path = tempfile.mkstemp(prefix="fetchbox-update-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return Path(path)
