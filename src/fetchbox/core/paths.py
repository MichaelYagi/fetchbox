"""Folder paths typed by people. Under WSL, Fetchbox is a Linux program but the person
thinks in Windows paths: ``C:\\Users\\me\\Media`` is ``/mnt/c/Users/me/Media`` here, and
``\\\\wsl.localhost\\Debian\\home\\me`` (how Windows reaches WSL) is ``/home/me``."""

from __future__ import annotations

import os
import platform
import re
import sys


def under_wsl() -> bool:
    if sys.platform != "linux":
        return False
    return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()


def host_path(path: str, *, wsl: bool | None = None) -> str:
    """The path as this machine knows it: under WSL, Windows forms become Linux ones."""
    p = path.strip()
    if not (under_wsl() if wsl is None else wsl):
        return p
    if m := re.match(r"^([A-Za-z]):(?:[\\/](.*))?$", p):
        rest = (m.group(2) or "").replace("\\", "/").strip("/")
        return f"/mnt/{m.group(1).lower()}" + (f"/{rest}" if rest else "")
    if m := re.match(r"^(?:\\\\|//)(?:wsl\.localhost|wsl\$)[\\/][^\\/]+(?:[\\/](.*))?$", p, re.I):
        rest = (m.group(1) or "").replace("\\", "/").strip("/")
        return "/" + rest
    return p
