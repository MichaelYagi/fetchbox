"""Entry point of the frozen (PyInstaller) builds.

Double-clicked with no arguments on Windows or macOS, it starts the tray; with
arguments it is the normal ``fetchbox`` command line (the tray starts the server by
running this same program as ``fetchbox serve``). The Linux build is command line only.
"""

import multiprocessing
import sys

from fetchbox.cli import main


def _console_to_file() -> None:
    """A windowed build (Windows, macOS) has no console: sys.stdout and sys.stderr are
    None, and anything writing to them fails (a failure PyInstaller then shows as a modal
    dialog nobody can see on a server). Send them to <data>/logs/console.log."""
    if sys.stdout is not None and sys.stderr is not None:
        return

    from fetchbox.config import resolve_data_dir

    args = sys.argv[1:]
    data = args[args.index("--data") + 1] if "--data" in args[:-1] else None
    logs = resolve_data_dir(data) / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        out = open(logs / "console.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        import os

        out = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = out
    if sys.stderr is None:
        sys.stderr = out


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _console_to_file()
    if len(sys.argv) == 1 and sys.platform in ("win32", "darwin"):
        sys.argv.append("tray")
    raise SystemExit(main())
