#!/usr/bin/env bash
# Build dist/Fetchbox.app (menu bar tray + server) for this Mac's architecture.
# Run from the repo root.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
(cd web && npm ci && npm run build)
rm -rf .venv-build build/pyi build/icons dist
"$PY" -m venv .venv-build
.venv-build/bin/pip install --quiet --upgrade pip
.venv-build/bin/pip install --quiet ./plugin-api ".[desktop]" "pyinstaller>=6.10"  # the plugin API: from here, not PyPI
.venv-build/bin/python packaging/make_icons.py build/icons
iconutil -c icns build/icons/fetchbox.iconset -o build/icons/fetchbox.icns
.venv-build/bin/pyinstaller packaging/fetchbox.spec --noconfirm --distpath dist --workpath build/pyi
rm -rf dist/Fetchbox  # the loose folder; the .app holds its own copy
echo "built dist/Fetchbox.app"
