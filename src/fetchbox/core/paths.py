"""Folders must be ones Fetchbox can open where it runs.

In WSL, any folder WSL opens is fine, the Windows drives under ``/mnt/c/...`` included.
What WSL can't open is refused, with what to use instead: a Windows-style path
(``C:\\...``, which WSL opens as ``/mnt/c/...``) and a ``\\\\server`` share (mount it in
WSL). On Windows, a WSL folder (``\\\\wsl.localhost\\...``) is refused.
"""

from __future__ import annotations

import os
import platform
import re
import sys

_DRIVE = re.compile(r"^([A-Za-z]):(?:[\\/](.*))?$")
_SHARE = re.compile(r"^(?:\\\\|//)")
_WSL_ON_WINDOWS = re.compile(r"^(?:\\\\|//)(?:wsl\.localhost|wsl\$)(?:[\\/]|$)", re.I)


def under_wsl() -> bool:
    if sys.platform != "linux":
        return False
    return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()


def native_folder_problem(
    path: str, *, wsl: bool | None = None, windows: bool | None = None
) -> str | None:
    """Why ``path`` can't be used here, or None when it can."""
    p = path.strip()
    in_wsl = under_wsl() if wsl is None else wsl
    on_windows = (sys.platform == "win32") if windows is None else windows
    if in_wsl and (m := _DRIVE.match(p)):
        rest = (m.group(2) or "").replace("\\", "/").strip("/")
        wsl_path = f"/mnt/{m.group(1).lower()}" + (f"/{rest}" if rest else "")
        drive = m.group(1)
        return f"WSL opens the {drive.upper()}: drive as /mnt/{drive.lower()}: use {wsl_path}"
    if in_wsl and _SHARE.match(p):
        return (
            "WSL can't open a \\\\server share directly: mount it in WSL (for example at "
            "/mnt/nas) and choose the folder there"
        )
    if on_windows and _WSL_ON_WINDOWS.match(p):
        return "Fetchbox runs on Windows: choose a Windows folder, not a WSL one"
    return None
