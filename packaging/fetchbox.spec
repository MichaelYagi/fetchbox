# PyInstaller spec for Fetchbox: one file per platform.
#   Windows: Fetchbox.exe (one file, tray, no console)
#   macOS:   Fetchbox.app (tray, menu bar only; a folder inside, as .apps are)
#   Linux:   fetchbox (command line, no tray: `fetchbox serve`)
# Build with packaging/build-<platform>, which installs Fetchbox (with its built web UI)
# into a fresh virtualenv first; this spec collects it from there.
# ruff: noqa
import os
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
    # bs4 loads its parser by name: the HTML responses of indexer definitions use lxml
    + ["bs4.builder._lxml", "lxml.etree", "lxml._elementpath", "lxml.html"]
    + ["python_multipart"]  # FastAPI imports it lazily for uploads (backups)
    # music tags: mutagen loads each format's module when it meets that format
    + collect_submodules("mutagen")
)
binaries = collect_dynamic_libs("libtorrent")
excludes = ["tkinter", "pytest", "mypy", "ruff"]
if TRAY:
    hidden += collect_submodules("pystray") + ["PIL.Image", "PIL.ImageDraw"]
    # The secrets key in Credential Manager / the Keychain: keyring finds its backends
    # through package metadata (entry points), so ship that too.
    hidden += collect_submodules("keyring")
    datas += copy_metadata("keyring")
    if sys.platform == "win32":
        hidden += collect_submodules("win32ctypes")
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
# Paths in a spec are relative to the spec's folder: the icons are built in <repo>/build.
ICONS = os.path.join(os.path.dirname(SPECPATH), "build", "icons")
icon = {"win32": "fetchbox.ico", "darwin": "fetchbox.icns"}.get(sys.platform)
icon = os.path.join(ICONS, icon) if icon else None

if sys.platform == "darwin":
    # A .app is a folder anyway: the "onedir" layout inside it (a one-file .app is
    # deprecated by PyInstaller and clashes with macOS's code-signing checks).
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Fetchbox", console=False,
              icon=icon, upx=False)  # fmt: skip
    coll = COLLECT(exe, a.binaries, a.datas, name="Fetchbox", upx=False)
    app = BUNDLE(
        coll,
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
else:
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
