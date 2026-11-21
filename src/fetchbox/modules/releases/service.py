"""Interactive search: query every indexer for a wanted item and decide each result.

Results are kept in an in-memory cache (``release_id`` -> release) for an hour, so a
grab can refer to a release a user picked from the list without resending it.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import FieldError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.ids import new_id
from fetchbox.modules.media.models import WantedItem
from fetchbox.modules.media.service import MediaService
from fetchbox.modules.profiles.service import ProfilesService, to_spec
from fetchbox.modules.releases.decision import Decision, DecisionContext, Target
from fetchbox.modules.releases.grabs import GrabsService, Reason, blocklist_keys, grabbed_keys
from fetchbox.modules.releases.models import Grab
from fetchbox.modules.releases.search import SearchOutcome, search
from fetchbox.modules.releases.targets import movie_target
from fetchbox.modules.sources.service import SourcesService
from fetchbox.modules.system.config import ConfigService
from fetchbox.providers.indexers import SearchQuery

CACHE_TTL = 3600.0
CACHE_SIZE = 5000


@dataclass(frozen=True)
class CachedRelease:
    release_id: str
    wanted_item_id: str
    decision: Decision
    expires: float


@dataclass(frozen=True)
class InteractiveResult:
    wanted_item_id: str
    indexers: int
    outcome: SearchOutcome
    release_ids: list[str]  # parallel to outcome.decisions


class ReleaseCache:
    def __init__(self, size: int = CACHE_SIZE) -> None:
        self.size = size
        self._data: OrderedDict[str, CachedRelease] = OrderedDict()

    def put(self, wanted_item_id: str, decision: Decision) -> str:
        rid = new_id()
        self._data[rid] = CachedRelease(rid, wanted_item_id, decision, time.monotonic() + CACHE_TTL)
        while len(self._data) > self.size:
            self._data.popitem(last=False)
        return rid

    def get(self, release_id: str) -> CachedRelease | None:
        hit = self._data.get(release_id)
        if hit is None or hit.expires < time.monotonic():
            self._data.pop(release_id, None)
            return None
        return hit


class ReleasesService:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        grabs: GrabsService,
        media: MediaService,
        profiles: ProfilesService,
        config: ConfigService,
        sources: SourcesService,
    ) -> None:
        self.db = db
        self.bus = bus
        self.grabs = grabs
        self.media = media
        self.profiles = profiles
        self.config = config
        self.sources = sources
        self.cache = ReleaseCache()

    async def _target(self, wanted: WantedItem) -> tuple[Target, SearchQuery, DecisionContext]:
        title = await self.media.get_title(wanted.title_id)
        if title.kind != "movie":
            raise ProblemError(
                422,
                "Validation Failed",
                "Searching for episodes arrives with the TV pipeline (Phase 2)",
                slug="validation",
                errors=[FieldError(field="wanted_item_id", message="series are not supported yet")],
            )
        target = movie_target(title)
        ids = {"tmdb": str(title.tmdb_id)} if title.tmdb_id else {}
        if title.imdb_id:
            ids["imdb"] = title.imdb_id
        query = SearchQuery("movie", (title.name,), year=title.year, ids=ids)
        profile = to_spec(await self.profiles.get_profile(title.profile_id))
        cfg = self.config.current.search
        async with self.db.sessionmaker() as s:
            blocked = await blocklist_keys(s, title.id)
            grabbed = await grabbed_keys(s, wanted.id)
        ctx = DecisionContext(
            profile=profile,
            formats=await self.profiles.format_specs(),
            blocklist=blocked,
            grabbed=grabbed,
            min_seeders=cfg.min_seeders,
            season_pack_share=cfg.season_pack_share,
        )
        return target, query, ctx

    async def interactive(self, wanted_item_id: str, *, actor: Actor) -> InteractiveResult:
        wanted = await self.media.get_wanted(wanted_item_id)
        target, query, ctx = await self._target(wanted)
        sources = await self.sources.release_sources()
        subject = Subject(type="wanted_item", id=wanted.id)
        await self.bus.emit(
            "search.started", subject=subject, actor=actor, name=target.names[0],
            mode="interactive", indexers=len(sources),
        )  # fmt: skip
        outcome = await search(sources, query, target, ctx)
        await self.media.mark_searched(wanted.id, utcnow())
        await self.bus.emit(
            "search.completed",
            subject=subject,
            actor=actor,
            name=target.names[0],
            mode="interactive",
            results=len(outcome.decisions),
            accepted=sum(d.accepted for d in outcome.decisions),
            failed_indexers=[f.source_name for f in outcome.failures],
        )
        ids = [self.cache.put(wanted.id, d) for d in outcome.decisions]
        return InteractiveResult(wanted.id, len(sources), outcome, ids)

    def cached(self, release_id: str) -> CachedRelease | None:
        return self.cache.get(release_id)

    async def grab(
        self,
        release_id: str,
        *,
        wanted_item_ids: list[str] | None,
        reason: Reason,
        allow_rejected: bool,
        actor: Actor,
    ) -> tuple[Grab, bool]:
        """Grab a release from a recent search. A rejected release needs ``allow_rejected``:
        manual grabs may override the decision engine, but only on purpose."""
        hit = self.cache.get(release_id)
        if hit is None:
            raise ProblemError(
                410,
                "Gone",
                f"release {release_id!r} is unknown or expired (results are kept for an "
                "hour); search again",
                slug="release-expired",
            )
        d = hit.decision
        if d.rejections and not allow_rejected:
            raise ProblemError(
                422,
                "Release Rejected",
                "the decision engine rejected this release; send allow_rejected: true to "
                "grab it anyway",
                slug="release-rejected",
                errors=[
                    FieldError(field="body.release_id", message=r.message) for r in d.rejections
                ],
            )
        return await self.grabs.grab(d, wanted_item_ids or [hit.wanted_item_id], reason=reason,
                                     actor=actor)  # fmt: skip
