"""Write THIRD-PARTY-NOTICES.txt for a frozen build: the license of everything inside it.

Run with the build's own Python, after installing Fetchbox there:

    python packaging/gen_third_party_notices.py dist/THIRD-PARTY-NOTICES.txt [--desktop]

Covered: every package Fetchbox needs at runtime (walked from its own requirements, with
the ``desktop`` extra for tray builds), Python itself, the PyInstaller bootloader, and the
web UI libraries compiled into the bundled interface (from ``web/node_modules``). A
package that ships no license file needs ``packaging/license-fallbacks/<name>.txt``;
without one the script fails, so nothing goes out with a notice missing.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
FALLBACKS = Path(__file__).resolve().parent / "license-fallbacks"
LICENSE_NAMES = re.compile(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.I)
# Compiled into the web UI bundle (the rest of web/ is build tooling only).
UI_RUNTIME = ["svelte", "@sveltejs/kit", "devalue", "esm-env", "clsx"]
LGPL_NOTE = (
    "pystray is licensed under the LGPL-3.0 and is included unmodified. Fetchbox's full\n"
    "source and build scripts are public (https://github.com/MichaelYagi/fetchbox), so\n"
    "you can rebuild this program with a modified or different pystray; see\n"
    "packaging/README.md there.\n"
)
PYINSTALLER_NOTE = (
    "This program was built with PyInstaller (https://pyinstaller.org). Its bootloader is\n"
    "licensed under the GPL-2.0 with a special exception allowing it to be distributed\n"
    "with programs under any license; see https://pyinstaller.org/en/stable/license.html.\n"
)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_dists(extras: set[str]) -> list[str]:
    """Fetchbox's runtime requirements, transitively, for this platform."""
    seen: set[str] = set()
    order: list[str] = []

    def walk(name: str, wanted: set[str]) -> None:
        key = _norm(name)
        if key in seen:
            return
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            return  # not installed here: not in this build either (platform marker)
        seen.add(key)
        if key != "fetchbox":
            order.append(dist.metadata["Name"])
        for raw in dist.requires or []:
            req = Requirement(raw)
            ok = req.marker is None or any(
                req.marker.evaluate({"extra": e}) for e in (wanted or {""})
            )
            if ok:
                walk(req.name, set(req.extras))

    walk("fetchbox", extras)
    return sorted(order, key=str.lower)


def license_of(name: str) -> tuple[str, str]:
    dist = distribution(name)
    meta = dist.metadata
    kind = meta.get("License-Expression") or ""
    if not kind:
        classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or []
                       if c.startswith("License ::")]  # fmt: skip
        lic = (meta.get("License") or "").strip()
        kind = ", ".join(classifiers) or (lic if len(lic) < 80 else lic.splitlines()[0])
    texts = []
    for f in dist.files or []:
        if (LICENSE_NAMES.search(str(f)) and ".dist-info" in str(f)) or (
            LICENSE_NAMES.search(Path(str(f)).name) and "/" not in str(f)
        ):
            with contextlib.suppress(OSError):
                texts.append(
                    Path(dist.locate_file(f)).read_text(encoding="utf-8", errors="replace")
                )
    fallback = FALLBACKS / f"{_norm(name)}.txt"
    if not texts and fallback.is_file():
        texts.append(fallback.read_text(encoding="utf-8"))
    if not texts:
        raise SystemExit(f"no license text for {name}: add {fallback.relative_to(ROOT)}")
    return kind or "see text", "\n\n".join(t.strip() for t in texts)


def ui_packages() -> list[tuple[str, str, str, str]]:
    out = []
    modules = ROOT / "web" / "node_modules"
    for name in UI_RUNTIME:
        pkg = modules / name
        if not (pkg / "package.json").is_file():
            continue
        info = json.loads((pkg / "package.json").read_text(encoding="utf-8"))
        files = [p for p in pkg.iterdir() if LICENSE_NAMES.search(p.name)]
        if not files:
            raise SystemExit(f"no license file for the web package {name}")
        text = "\n\n".join(p.read_text(encoding="utf-8", errors="replace").strip() for p in files)
        out.append((name, info.get("version", "?"), str(info.get("license", "?")), text))
    return out


def main() -> None:
    target = Path(sys.argv[1])
    desktop = "--desktop" in sys.argv
    parts = [
        "Fetchbox includes the following third-party software, each under its own license.\n"
        "Fetchbox itself is MIT-licensed (LICENSE.txt); source: "
        "https://github.com/MichaelYagi/fetchbox\n"
    ]

    def section(title: str, kind: str, text: str) -> None:
        parts.append(f"\n{'=' * 78}\n{title}\nLicense: {kind}\n{'-' * 78}\n{text.strip()}\n")

    py_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not py_license.is_file():
        py_license = (
            Path(sys.base_prefix)
            / "lib"
            / f"python{sys.version_info[0]}.{sys.version_info[1]}"
            / "LICENSE.txt"
        )
    section(
        f"Python {sys.version.split()[0]}",
        "PSF-2.0",
        py_license.read_text(encoding="utf-8", errors="replace")
        if py_license.is_file()
        else "Python Software Foundation License: https://docs.python.org/3/license.html",
    )
    section("PyInstaller bootloader", "GPL-2.0 with bootloader exception", PYINSTALLER_NOTE)
    names = runtime_dists({"desktop"} if desktop else set())
    for name in names:
        kind, text = license_of(name)
        if _norm(name) == "pystray":
            text = LGPL_NOTE + "\n" + text
        section(f"{name} {distribution(name).version}", kind, text)
    for name, ver, kind, text in ui_packages():
        section(f"{name} {ver} (web interface)", kind, text)
    target.write_text("".join(parts), encoding="utf-8")
    print(f"{target}: {len(names)} Python packages, {len(ui_packages())} web packages")


if __name__ == "__main__":
    main()
