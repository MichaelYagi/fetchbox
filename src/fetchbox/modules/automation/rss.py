"""RSS sync: every 15 minutes, read each indexer's newest releases and grab the ones a
wanted movie or episode is waiting for.

A search asks indexers about one title; RSS goes the other way round and is how new
episodes get picked up within minutes of release, without searching every show. Each
release is parsed, matched to a library title by name (and year for movies), and the
wanted items it would cover (monitored, aired or available, not busy) go through the
same decisions and grab as a search, so quality profiles, blocklist and upgrades apply
the same way. RSS never moves an item's next scheduled search.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import structlog

from fetchbox.core.db import utcnow
from fetchbox.modules.media.models import Title
from fetchbox.modules.media.series import aired
from fetchbox.modules.releases.decision import Candidate, title_key
from fetchbox.modules.releases.parser import ParsedRelease, parse_release
from fetchbox.providers.indexers import IndexerError

if TYPE_CHECKING:
    from fetchbox.modules.automation.search import AutoSearch
    from fetchbox.modules.sources.service import IndexerSource

log = structlog.get_logger(__name__)

RSS_JOB = "rss.sync"
FEED_TIMEOUT = 60.0
YEAR_TOLERANCE = 1


def _names(t: Title) -> list[str]:
    meta = t.meta or {}
    return [n for n in (t.name, meta.get("original_name"), *(meta.get("alt_titles") or [])) if n]


def is_episode(p: ParsedRelease) -> bool:
    return bool(p.seasons or p.episodes or p.air_date or p.absolute_episodes or p.complete_series)


async def _feeds(auto: AutoSearch) -> tuple[list[Candidate], list[str], int]:
    """Every enabled indexer's feed: (releases, failed indexer names, indexers read)."""
    sources = await auto.sources.release_sources()

    async def one(src: IndexerSource) -> list[Candidate] | str:
        try:
            found = await asyncio.wait_for(src.provider.rss(), FEED_TIMEOUT)
        except NotImplementedError:  # this kind of indexer has no feed
            return []
        except TimeoutError:
            return src.name
        except IndexerError as exc:
            await auto.sources.record_failure(src.id, exc)
            return src.name
        except Exception as exc:  # a broken feed mustn't stop the others
            log.warning("rss.feed_failed", source=src.name, error=str(exc))
            return src.name
        await auto.sources.record_success(src.id)
        return [
            c.model_copy(update={"source_id": src.id, "source_name": src.name,
                                 "priority": src.priority})
            for c in found
        ]  # fmt: skip

    results = await asyncio.gather(*(one(s) for s in sources))
    found = [c for r in results if isinstance(r, list) for c in r]
    failed = [r for r in results if isinstance(r, str)]
    return found, failed, len(sources)


async def rss_sync(auto: AutoSearch) -> dict[str, Any]:
    releases, failed, n_sources = await _feeds(auto)
    titles = await auto.media.all_titles()
    wanted = await auto.media.wanted_for([t.id for t in titles if t.kind == "movie"])
    index: dict[str, list[Title]] = defaultdict(list)
    for t in titles:
        for key in {title_key(n) for n in _names(t)}:
            index[key].append(t)

    # releases per (title, season); None: a movie, or episodes found by date/number
    groups: dict[tuple[str, int | None], list[tuple[Candidate, ParsedRelease]]] = defaultdict(list)
    by_id = {t.id: t for t in titles}
    for c in releases:
        p = parse_release(c.title)
        if p.sample:
            continue
        tv = is_episode(p)
        seen: set[str] = set()
        for key in {title_key(n) for n in (p.title, *p.alt_titles)}:
            for t in index.get(key, []):
                if t.id in seen or (t.kind == "series") != tv:
                    continue
                if (
                    t.kind == "movie"
                    and p.year
                    and t.year
                    and abs(p.year - t.year) > YEAR_TOLERANCE
                ):
                    continue
                seen.add(t.id)
                season = p.seasons[0] if t.kind == "series" and len(p.seasons) == 1 else None
                groups[(t.id, season)].append((c, p))

    today = utcnow().date()
    grabbed: list[str] = []
    for (title_id, season), found in groups.items():
        title = by_id[title_id]
        if title.kind == "movie":
            items = wanted.get(title_id, [])
        else:
            rows = await auto.media.episode_items(title_id, season=season)
            keys: set[tuple[int, int]] = set()
            packs = False
            for _, p in found:
                if p.seasons and not p.episodes:
                    packs = True  # a season pack: any aired episode of that season
                keys |= {(p.seasons[0], e) for e in p.episodes if p.seasons}
            dates = {p.air_date for _, p in found if p.air_date}
            absolute = {a for _, p in found for a in p.absolute_episodes}
            items = [
                w
                for n, ep, w in rows
                if aired(ep.air_date, today)
                and (
                    packs
                    or (n, ep.number) in keys
                    or ep.air_date in dates
                    or (ep.absolute_number is not None and ep.absolute_number in absolute)
                )
            ]
        if not items:
            continue
        result = await auto._search(
            items, reason="automatic", season_query=False, feed=[c for c, _ in found]
        )
        if result.get("grabbed"):
            grabbed += [g["release_title"] for g in result.get("grabs", [])] or [
                result["release_title"]
            ]
    out = {
        "indexers": n_sources,
        "releases": len(releases),
        "matched": len(groups),
        "grabbed": grabbed,
        "failed_indexers": failed,
    }
    log.info("rss.synced", **out)
    return out
