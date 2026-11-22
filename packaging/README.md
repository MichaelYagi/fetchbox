# Packaging

Desktop builds are one file per platform, made with PyInstaller from `fetchbox.spec`:

| Platform | Script | Output |
| --- | --- | --- |
| Windows 10/11 x64 | `packaging\build-windows.ps1` (PowerShell, not WSL) | `dist\Fetchbox.exe`: tray + server |
| macOS (Apple Silicon or Intel: builds for the Mac it runs on) | `packaging/build-macos.sh` | `dist/Fetchbox.app`: menu bar + server |
| Linux x86_64 | `packaging/build-linux.sh` | `dist/fetchbox`: command line server |

Each script builds the web UI, installs Fetchbox into a fresh `.venv-build`, and runs
PyInstaller. Then add the notices that must travel with a build:

    cp LICENSE dist/LICENSE.txt
    .venv-build/bin/python packaging/gen_third_party_notices.py dist/THIRD-PARTY-NOTICES.txt [--desktop]

(`--desktop` for the Windows and macOS builds, which include the tray.) A runtime
dependency that ships no license file needs `license-fallbacks/<name>.txt`, or the
notices script stops.

Check a build before handing it out (scratch data folder, never your real one):

    .venv-build/bin/python packaging/smoke_test.py dist/fetchbox v0.1.0
    # macOS: dist/Fetchbox.app/Contents/MacOS/Fetchbox; Windows: dist\Fetchbox.exe

## Installers

Made from a finished build (with its README, LICENSE and notices in `dist/`):

| Platform | Command | Output |
| --- | --- | --- |
| Windows | `iscc /DAppVersion=1.2.3 /FFetchbox-Setup packaging\windows\fetchbox.iss` (Inno Setup 6; also needs `build\icons`) | a per-user installer: no admin prompt, Start menu entry, optional start at sign-in, uninstaller that asks before deleting the data |
| macOS | `packaging/macos/make_dmg.sh Fetchbox.dmg` | a disk image: drag Fetchbox to Applications |
| Linux | `packaging/linux/make_packages.sh 1.2.3 Fetchbox` | `Fetchbox.deb`: a systemd service as the `fetchbox` user, data in `/var/lib/fetchbox`, settings in `/etc/default/fetchbox`; and `Fetchbox.AppImage`: one file, runs `serve` with no arguments |

The AppImage tools are pinned releases, checked by SHA-256. The `.deb` requires at least
the glibc of the machine it was built on. The release workflow installs, runs and
uninstalls each installer before publishing.

## Releases

Releases follow the same flow as roast-telemetry:

1. Set the version in `pyproject.toml` and `web/package.json`, commit, then
   `git tag vX.Y.Z && git push origin vX.Y.Z`.
2. `release.yml` here runs the tests and checks the tag matches the version. It then
   publishes a GitHub Release with the wheel, the sdist and a source zip.
3. `notify-desktop.yml` asks the private `MichaelYagi/fetchbox-artifacts` repo to build
   the desktop apps for that tag. This needs a `DESKTOP_DISPATCH_TOKEN` secret here: a
   fine-grained token with "Contents: Read and write" on fetchbox-artifacts only.
   Without the secret it skips itself with a warning.
4. That repo's `desktop-build.yml` builds Windows, macOS Silicon, macOS Intel and Linux.
   It smoke-tests each build, makes and tests the installers, and publishes them as a
   Release of the private repo.

## The tray and pystray (LGPL-3.0)

The tray uses pystray unmodified. Because the source and these scripts are public, you
can rebuild with a different pystray: install it into `.venv-build` after the
`pip install` step, then run the PyInstaller line again.
