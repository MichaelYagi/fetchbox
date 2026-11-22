"""Start a built Fetchbox with a scratch data folder, check it answers and reports the
expected version, then stop it.
Usage: python smoke_test.py <executable> <vX.Y.Z | X.Y.Z | any>"""

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

exe, want = sys.argv[1], sys.argv[2].lstrip("v")
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
data = tempfile.mkdtemp(prefix="fetchbox-smoke-")
env = {**os.environ, "FETCHBOX_DOWNLOADS_ROOT": os.path.join(data, "Downloads"),
       "FETCHBOX_SEED_INDEXERS": "0"}  # fmt: skip
proc = subprocess.Popen([exe, "--data", data, "serve", "--port", str(port)], env=env)
base = f"http://127.0.0.1:{port}"


def show_logs() -> None:
    """What the server wrote (a windowed build has no console to show it)."""
    for f in sorted(Path(data, "logs").glob("*.log")):
        print(f"---- {f.name} (last 60 lines) ----")
        print("\n".join(f.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]))


try:
    deadline = time.monotonic() + 120
    while True:
        if proc.poll() is not None:
            show_logs()
            sys.exit(f"the server exited with code {proc.returncode}")
        try:
            with urllib.request.urlopen(f"{base}/api/v1/system/ping", timeout=2) as r:
                if r.status == 200:
                    break
        except OSError:
            pass
        if time.monotonic() > deadline:
            show_logs()
            sys.exit("the server didn't answer within 120 s")
        time.sleep(1)
    with urllib.request.urlopen(f"{base}/api/v1/openapi.json", timeout=5) as r:
        import json

        got = json.load(r)["info"]["version"]
    with urllib.request.urlopen(f"{base}/", timeout=5) as r:
        page = r.read().decode("utf-8", "replace")
    assert want == "any" or got == want, f"version {got!r}, expected {want!r}"
    assert "<html" in page.lower(), "the web interface isn't served"
    if sys.platform == "win32":
        # the Windows app keeps the secrets key in Credential Manager, not in a file
        assert not Path(data, "secrets.key").exists(), "secrets.key written: keyring not used"
        print("ok: the secrets key is in Windows Credential Manager")
    print(f"ok: Fetchbox {got} answered at {base} and serves its web interface")
finally:
    proc.terminate()
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        proc.kill()
    if sys.platform == "win32":
        # A one-file Windows build is two processes (launcher and the program); the
        # second would keep Fetchbox.exe locked for whatever runs next.
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
