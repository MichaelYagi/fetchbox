"""GET /system/diagnostics: a bug-report zip without secrets."""

from __future__ import annotations

import io
import json
import logging
import zipfile

import httpx

from fetchbox.modules.system.router import redact
from tests.fake_tmdb import V3_KEY
from tests.pipeline import Fakes, _indexer


def test_redact() -> None:
    assert redact("GET /api?apikey=abcdef123456&t=search") == "GET /api?apikey=[redacted]&t=search"
    assert redact('{"password": "hunter2hunter2"}') == '{"password": "[redacted]"}'
    assert redact(f"key {V3_KEY} used") == "key [redacted-hex] used"
    assert redact("Bearer fb_abc12345_secretpart") == "Bearer fb_[redacted]"
    assert redact("Dune.2021.1080p") == "Dune.2021.1080p"


async def test_diagnostics_zip_has_no_secrets(c: httpx.AsyncClient, fakes: Fakes) -> None:
    await _indexer(c, fakes, "one", [])
    logging.getLogger("fetchbox.test").warning("calling http://x/api?apikey=%s", V3_KEY)
    r = await c.get("/system/diagnostics")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = set(z.namelist())
        assert names == {
            "versions.json",
            "health.json",
            "config.json",
            "setup.json",
            "fetchbox.log",
        }
        everything = b"".join(z.read(n) for n in names).decode()
        versions = json.loads(z.read("versions.json"))
        setup = json.loads(z.read("setup.json"))
    assert V3_KEY not in everything  # the TMDB key: masked in config, scrubbed in logs
    assert versions["schema_revision"] == versions["schema_head"]
    assert setup["indexers"][0]["kind"] == "torznab" and "url" not in setup["indexers"][0]
