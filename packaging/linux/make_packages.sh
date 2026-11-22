#!/usr/bin/env bash
# Package dist/fetchbox (from build-linux.sh) as a .deb (a systemd service) and an
# AppImage (one file that runs anywhere). Run from the repo root.
#   packaging/linux/make_packages.sh <version: 1.2.3> <output name, no extension>
# Needs dpkg-deb, and network access once for the pinned AppImage tools.
set -euo pipefail
cd "$(dirname "$0")/../.."
VERSION="$1"; OUT="$2"
HERE=packaging/linux
WORK=build/linux-pkg
rm -rf "$WORK"; mkdir -p "$WORK"

# The icon (Pillow only, installed after PyInstaller ran: not part of the binary)
.venv-build/bin/pip install --quiet pillow
.venv-build/bin/python packaging/make_icons.py build/icons >/dev/null
.venv-build/bin/python -c "from PIL import Image; Image.open('build/icons/fetchbox.png').resize((256, 256)).save('$WORK/fetchbox.png')"

# ---- .deb ---------------------------------------------------------------------------
DEB="$WORK/deb"
install -D -m 0755 dist/fetchbox "$DEB/usr/bin/fetchbox"
install -D -m 0644 "$HERE/fetchbox.service" "$DEB/lib/systemd/system/fetchbox.service"
install -D -m 0644 "$HERE/default" "$DEB/etc/default/fetchbox"
install -D -m 0644 "$WORK/fetchbox.png" "$DEB/usr/share/icons/hicolor/256x256/apps/fetchbox.png"
install -D -m 0644 dist/LICENSE.txt "$DEB/usr/share/doc/fetchbox/copyright"
install -D -m 0644 dist/THIRD-PARTY-NOTICES.txt "$DEB/usr/share/doc/fetchbox/THIRD-PARTY-NOTICES.txt"
install -D -m 0644 dist/README.txt "$DEB/usr/share/doc/fetchbox/README.txt"
mkdir -p "$DEB/DEBIAN"
for f in postinst prerm postrm; do install -m 0755 "$HERE/$f" "$DEB/DEBIAN/$f"; done
echo /etc/default/fetchbox > "$DEB/DEBIAN/conffiles"
# The binary needs at least the C library it was built against
GLIBC="$(getconf GNU_LIBC_VERSION | cut -d' ' -f2)"  # e.g. "glibc 2.39"
cat > "$DEB/DEBIAN/control" <<CONTROL
Package: fetchbox
Version: $VERSION
Architecture: amd64
Maintainer: Fetchbox <noreply@fetchbox.invalid>
Depends: libc6 (>= $GLIBC), adduser
Section: net
Priority: optional
Homepage: https://github.com/MichaelYagi/fetchbox
Installed-Size: $(du -sk "$DEB" | cut -f1)
Description: Find and download movies and TV
 Fetchbox searches indexers, downloads with its built-in torrent engine and files
 movies and episodes into your library, with a web interface. Runs as the
 fetchbox service on http://127.0.0.1:8787; settings in /etc/default/fetchbox.
CONTROL
dpkg-deb --root-owner-group -Zxz --build "$DEB" "$OUT.deb" >/dev/null
echo "built $OUT.deb"

# ---- AppImage -------------------------------------------------------------------------
TOOLS=build/appimage-tools
mkdir -p "$TOOLS"
fetch() {  # url, file, sha256: pinned releases, checked
    [ -f "$TOOLS/$2" ] || curl -sSfLo "$TOOLS/$2" "$1"
    echo "$3  $TOOLS/$2" | sha256sum -c --quiet - || { rm -f "$TOOLS/$2"; exit 1; }
}
fetch https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage \
    appimagetool ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
# The static runtime: no libfuse2 needed on the user's machine
fetch https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64 \
    runtime 2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d
chmod +x "$TOOLS/appimagetool"
APP="$WORK/AppDir"
install -D -m 0755 dist/fetchbox "$APP/usr/bin/fetchbox"
install -m 0755 "$HERE/AppRun" "$APP/AppRun"
install -m 0644 "$HERE/fetchbox.desktop" "$APP/fetchbox.desktop"
install -m 0644 "$WORK/fetchbox.png" "$APP/fetchbox.png"
install -D -m 0644 dist/LICENSE.txt "$APP/usr/share/doc/fetchbox/LICENSE.txt"
install -D -m 0644 dist/THIRD-PARTY-NOTICES.txt "$APP/usr/share/doc/fetchbox/THIRD-PARTY-NOTICES.txt"
ARCH=x86_64 VERSION="$VERSION" APPIMAGE_EXTRACT_AND_RUN=1 "$TOOLS/appimagetool" --no-appstream \
    --runtime-file "$TOOLS/runtime" "$APP" "$OUT.AppImage" >"$WORK/appimagetool.log" 2>&1 \
    || { cat "$WORK/appimagetool.log"; exit 1; }
chmod +x "$OUT.AppImage"
echo "built $OUT.AppImage"
