# PyInstaller spec for Fetchbox: one file per platform.
#   Windows: Fetchbox.exe (tray, no console)    macOS: Fetchbox.app (tray, menu bar only)
#   Linux:   fetchbox (command line, no tray: `fetchbox serve`)
# Build with packaging/build-<platform>, which installs Fetchbox (with its built web UI)
# into a fresh virtualenv first; this spec collects it from there.
# ruff: noqa
import sys
from importlib.metadata import version

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

TRAY = sys.platform in ("win32", "darwin")
VERSION = version("fetchbox")

datas = (
    collect_data_files("fetchbox")  # web UI (static/), indexer definitions, ...
    # Alembic reads migration scripts from disk at startup: ship them as files too.
    + collect_data_files("fetchbox.migrations", include_py_files=True)
    + copy_metadata("fetchbox")  # version and the providers' entry points
)
hidden = (
    collect_submodules("fetchbox")
    + collect_submodules("uvicorn")
    + collect_submodules("sqlalchemy.dialects.sqlite")
    + ["aiosqlite", "libtorrent", "argon2", "psutil"]
)
binaries = collect_dynamic_libs("libtorrent")
excludes = ["tkinter", "pytest", "mypy", "ruff"]
if TRAY:
    hidden += collect_submodules("pystray") + ["PIL.Image", "PIL.ImageDraw"]
else:
    hidden = [h for h in hidden if h != "fetchbox.tray"]
    excludes += ["pystray", "PIL", "Xlib"]

a = Analysis(
    ["entry.py"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = {"win32": "build/icons/fetchbox.ico", "darwin": "build/icons/fetchbox.icns"}.get(sys.platform)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Fetchbox" if TRAY else "fetchbox",
    console=not TRAY,
    icon=icon,
    upx=False,
)
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="Fetchbox.app",
        icon=icon,
        bundle_identifier="dev.fetchbox.Fetchbox",
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,  # a menu bar app: no Dock icon
            "NSHighResolutionCapable": True,
        },
    )
