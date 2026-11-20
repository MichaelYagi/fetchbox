"""Freeze-smoke test: proves libtorrent imports and a session starts inside a frozen build."""

import sys

import libtorrent as lt

s = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False})
ok = s.is_listening()
print(f"libtorrent {lt.__version__} listening={ok} python={sys.version.split()[0]}")
sys.exit(0 if ok else 1)
