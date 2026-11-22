"""Folders must be native to where Fetchbox runs.

In WSL, only WSL folders: a Windows folder (``C:\\...``, ``/mnt/c/...``, a ``\\\\server``
share) is refused. On Windows, only Windows folders: a WSL folder
(``\\\\wsl.localhost\\...``) is refused. Mixing the two puts downloads and the library
on different systems; here they stay on one.
"""

from __future__ import annotations

import os
import platform
import re
import sys

_WINDOWS_IN_WSL = re.compile(r"^(?:[A-Za-z]:(?:[\\/]|$)|/mnt/[A-Za-z](?:/|$)|\\\\|//)")
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
    if in_wsl and _WINDOWS_IN_WSL.match(p):
        return "Fetchbox runs in WSL: choose a WSL folder (like /home/you/Media), not a Windows one"
    if on_windows and _WSL_ON_WINDOWS.match(p):
        return "Fetchbox runs on Windows: choose a Windows folder, not a WSL one"
    return None
