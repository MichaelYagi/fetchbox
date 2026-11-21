"""web/openapi.json (the web UI's typed client is generated from it) matches the API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fetchbox.app import create_app

SNAPSHOT = Path(__file__).parent.parent / "web" / "openapi.json"


def test_web_client_schema_is_current(settings: Any) -> None:
    current = create_app(settings).openapi()
    saved = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert saved == current, (
        "the API changed: run `npm run gen` in web/ and commit openapi.json + schema.d.ts"
    )
