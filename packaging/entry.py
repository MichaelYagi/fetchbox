"""Entry point of the frozen (PyInstaller) builds.

Double-clicked with no arguments on Windows or macOS, it starts the tray; with
arguments it is the normal ``fetchbox`` command line (the tray starts the server by
running this same program as ``fetchbox serve``). The Linux build is command line only.
"""

import multiprocessing
import sys

from fetchbox.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) == 1 and sys.platform in ("win32", "darwin"):
        sys.argv.append("tray")
    raise SystemExit(main())
