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

exe, want = sys.argv[1], sys.argv[2].lstrip("v")
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
data = tempfile.mkdtemp(prefix="fetchbox-smoke-")
env = {**os.environ, "FETCHBOX_DOWNLOADS_ROOT": os.path.join(data, "Downloads"),
       "FETCHBOX_SEED_INDEXERS": "0"}  # fmt: skip
proc = subprocess.Popen([exe, "--data", data, "serve", "--port", str(port)], env=env)
base = f"http://127.0.0.1:{port}"
try:
    deadline = time.monotonic() + 120
    while True:
        if proc.poll() is not None:
            sys.exit(f"the server exited with code {proc.returncode}")
        try:
            with urllib.request.urlopen(f"{base}/api/v1/system/ping", timeout=2) as r:
                if r.status == 200:
                    break
        except OSError:
            pass
        if time.monotonic() > deadline:
            sys.exit("the server didn't answer within 120 s")
        time.sleep(1)
    with urllib.request.urlopen(f"{base}/api/v1/openapi.json", timeout=5) as r:
        import json

        got = json.load(r)["info"]["version"]
    with urllib.request.urlopen(f"{base}/", timeout=5) as r:
        page = r.read().decode("utf-8", "replace")
    assert want == "any" or got == want, f"version {got!r}, expected {want!r}"
    assert "<html" in page.lower(), "the web interface isn't served"
    print(f"ok: Fetchbox {got} answered at {base} and serves its web interface")
finally:
    proc.terminate()
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        proc.kill()
