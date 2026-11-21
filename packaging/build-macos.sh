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
.venv-build/bin/pip install --quiet ".[desktop]" "pyinstaller>=6.10"
.venv-build/bin/python packaging/make_icons.py build/icons
iconutil -c icns build/icons/fetchbox.iconset -o build/icons/fetchbox.icns
.venv-build/bin/pyinstaller packaging/fetchbox.spec --noconfirm --distpath dist --workpath build/pyi
rm -f dist/Fetchbox  # the bare executable; the .app holds its own copy
echo "built dist/Fetchbox.app"
