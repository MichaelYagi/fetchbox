"""``python -m fetchbox_plugin check|new ...``: the plugin tools without Fetchbox (``fetchbox
plugin ...`` runs the same ones)."""

import sys

from fetchbox_plugin import check, new

commands = {"check": check.main, "new": new.main}
args = sys.argv[1:]
if not args or args[0] not in commands:
    print("usage: python -m fetchbox_plugin check <path> | new <name> [--kind addon|media]")
    sys.exit(2)
sys.exit(commands[args[0]](args))
