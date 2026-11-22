"""``fetchbox corpus capture``: collect real release names from your indexers.

The parser's test corpus (``tests/fixtures/releases/*.jsonl``) is hand-written from scene
and P2P conventions; real feeds are messier. This reads every enabled indexer's newest
releases (and, with ``--search``, search results) through the same providers the server
uses, and writes one JSON line per new name with how Fetchbox parses it today:

    fetchbox --data ./data corpus capture --out captured.jsonl --search "Dune" --limit 400

Review the file: lines parsed correctly become test cases as they are (drop ``parsed``,
keep the fields you checked); wrong ones become cases with the right values, or
``"xfail": "why"`` until the parser handles them. Nothing is sent anywhere but your own
indexers, and nothing is changed in Fetchbox.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fetchbox.config import Settings
from fetchbox.providers.indexers import IndexerError, SearchQuery

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "releases"
KEEP = ("title", "year", "seasons", "episodes", "absolute_episodes", "air_date", "resolution",
        "source", "modifier", "video_codec", "hdr", "audio_codecs", "group", "edition",
        "languages", "proper", "repack", "quality")  # fmt: skip


def _known() -> set[str]:
    names: set[str] = set()
    for f in FIXTURES.glob("*.jsonl") if FIXTURES.is_dir() else []:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("{"):
                names.add(json.loads(line).get("name", ""))
    return names


async def capture(settings: Settings, out: Path, searches: list[str], limit: int) -> int:
    import fetchbox.providers.builtin  # noqa: F401  (registers the providers)
    from fetchbox.core.db import Database
    from fetchbox.core.events import EventBus
    from fetchbox.core.providers import registry
    from fetchbox.core.secrets import load_or_create
    from fetchbox.modules.releases.parser import parse_release
    from fetchbox.modules.sources.service import SourcesService

    box, _ = load_or_create(settings.data_dir)
    db = Database(settings.database_url)
    sources = SourcesService(db, EventBus(), box, registry)
    known = _known()
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    try:
        for src in await sources.release_sources():
            calls: list[Any] = [src.provider.rss()]
            for term in searches:
                calls.append(src.provider.search(SearchQuery("movie", (term,))))
                calls.append(src.provider.search(SearchQuery("series", (term,))))
            for res in await asyncio.gather(*calls, return_exceptions=True):
                if isinstance(res, (IndexerError, NotImplementedError)):
                    print(f"{src.name}: {getattr(res, 'message', 'no feed')}")
                    continue
                if isinstance(res, BaseException):
                    raise res
                for c in res:
                    if c.title in known or c.title in seen or len(rows) >= limit:
                        continue
                    seen.add(c.title)
                    p = parse_release(c.title).model_dump(mode="json")
                    parsed = {k: p[k] for k in KEEP if p.get(k) not in (None, [], False)}
                    rows.append({"name": c.title, "indexer": src.name, "parsed": parsed})
        await sources.aclose()
    finally:
        await db.dispose()
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    await asyncio.to_thread(out.write_text, text, "utf-8")
    return len(rows)
