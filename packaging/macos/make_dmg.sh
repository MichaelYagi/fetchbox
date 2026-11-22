#!/usr/bin/env bash
# Package dist/Fetchbox.app (from build-macos.sh, plus the README, LICENSE and notices
# the release workflow adds to dist/) as a disk image: open it, drag Fetchbox to
# Applications. Run from the repo root.
#   packaging/macos/make_dmg.sh <output.dmg>
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT="$1"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
ditto dist/Fetchbox.app "$STAGE/Fetchbox.app"
ln -s /Applications "$STAGE/Applications"
for f in README.txt LICENSE.txt THIRD-PARTY-NOTICES.txt; do
    [ -f "dist/$f" ] && cp "dist/$f" "$STAGE/"
done
rm -f "$OUT"
hdiutil create -volname Fetchbox -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$OUT" >/dev/null
echo "built $OUT"
