#!/usr/bin/env bash
# Build dist/fetchbox (Linux x86_64 command line, no tray). Run from the repo root.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
(cd web && npm ci && npm run build)
rm -rf .venv-build build/pyi dist
"$PY" -m venv .venv-build
.venv-build/bin/pip install --quiet --upgrade pip
.venv-build/bin/pip install --quiet ./plugin-api . "pyinstaller>=6.10"  # the plugin API: from here, not PyPI
.venv-build/bin/pyinstaller packaging/fetchbox.spec --noconfirm --distpath dist --workpath build/pyi
echo "built dist/fetchbox"
