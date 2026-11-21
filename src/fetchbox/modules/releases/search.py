"""Fan a search out to every release source, then decide and rank the results.

Sources (Torznab and YAML indexers, later) implement ``ReleaseSource``. One slow or
failing source never fails the search: it is reported in ``SearchOutcome.failures``
and the others' results are still decided.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import structlog

from fetchbox.modules.releases.decision import (
    Candidate,
    Decision,
    DecisionContext,
    Target,
    decide_all,
    select,
)
from fetchbox.providers.indexers import SearchQuery

log = structlog.get_logger(__name__)


class ReleaseSource(Protocol):
    id: str
    name: str
    priority: int  # lower wins ties

    async def search(self, query: SearchQuery) -> list[Candidate]: ...


@dataclass(frozen=True)
class SourceFailure:
    source_id: str
    source_name: str
    error: str


@dataclass(frozen=True)
class SearchOutcome:
    decisions: list[Decision]  # ranked, rejected ones included
    picks: list[Decision]  # what an automatic search grabs
    failures: list[SourceFailure]


def dedupe(candidates: Sequence[Candidate]) -> list[Candidate]:
    """The same torrent from several indexers is listed once, from the best indexer."""
    best: dict[str, Candidate] = {}
    out: list[Candidate] = []
    for c in candidates:
        if not c.info_hash:
            out.append(c)
            continue
        h = c.info_hash.lower()
        prev = best.get(h)
        if prev is None or (c.priority, -(c.seeders or 0)) < (prev.priority, -(prev.seeders or 0)):
            best[h] = c
    return out + list(best.values())


async def search(
    sources: Sequence[ReleaseSource],
    query: SearchQuery,
    target: Target,
    ctx: DecisionContext,
    *,
    source_timeout: float = 30.0,
) -> SearchOutcome:
    async def one(src: ReleaseSource) -> list[Candidate] | SourceFailure:
        try:
            found = await asyncio.wait_for(src.search(query), source_timeout)
        except TimeoutError:
            return SourceFailure(src.id, src.name, f"timed out after {source_timeout:g}s")
        except Exception as exc:
            log.warning("search.source_failed", source=src.name, error=str(exc))
            return SourceFailure(src.id, src.name, str(exc) or type(exc).__name__)
        return [
            c.model_copy(update={"source_id": src.id, "source_name": src.name,
                                 "priority": src.priority})
            for c in found
        ]  # fmt: skip

    results = await asyncio.gather(*(one(s) for s in sources))
    candidates = [c for r in results if isinstance(r, list) for c in r]
    failures = [r for r in results if isinstance(r, SourceFailure)]
    decisions = decide_all(dedupe(candidates), target, ctx)
    return SearchOutcome(decisions, select(decisions, target), failures)
