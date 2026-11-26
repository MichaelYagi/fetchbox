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


def test_no_two_api_models_share_a_name() -> None:
    """Two models with one name make FastAPI rename both (module__path__Name), which silently
    changes an existing schema (it happened with JobOut and KindOut). No new ones."""
    import json
    from pathlib import Path

    doc = json.loads((Path(__file__).parents[1] / "web" / "openapi.json").read_text("utf-8"))
    clashing = {name for name in doc["components"]["schemas"] if "__" in name}
    assert clashing <= KNOWN_CLASHES, sorted(clashing - KNOWN_CLASHES)


# Older clashes, already in the API (renaming them now would change it)
KNOWN_CLASHES = {
    "fetchbox__modules__downloads__router__ReorderIn",
    "fetchbox__modules__downloads__service__PathMapping",
    "fetchbox__modules__integrations__service__PathMapping",
    "fetchbox__modules__library__router__ImportIn",
    "fetchbox__modules__sources__router__ReorderIn",
    "fetchbox__modules__system__config_router__ImportIn",
}
