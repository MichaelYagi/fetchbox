"""Search: query every indexer for wanted items and decide each result.

A search covers one movie, one episode, or episodes of one season (a season search asks
indexers for the whole season, so season packs come back too; the decision engine then
prefers packs and fills gaps with single episodes).

Interactive search returns everything; automatic search (``modules.automation``) grabs
the top pick. Both judge an acquired item's results against its current file, so only
real upgrades are accepted. Interactive results are kept in an in-memory cache
(``release_id`` -> release) for an hour, so a grab can refer to a release a user picked
from the list without resending it.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select

from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import FieldError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.ids import new_id
from fetchbox.modules.library.models import MediaFile, MediaFileItem
from fetchbox.modules.media.models import Title, WantedItem
from fetchbox.modules.media.service import MediaService
from fetchbox.modules.profiles.service import ProfilesService, to_spec
from fetchbox.modules.releases.decision import (
    Candidate,
    Decision,
    DecisionContext,
    EpisodeKey,
    Existing,
    Target,
    WantedEpisode,
    decide_all,
)
from fetchbox.modules.releases.decision import select as pick_releases
from fetchbox.modules.releases.grabs import GrabsService, Reason, blocklist_keys, grabbed_keys
from fetchbox.modules.releases.models import Grab
from fetchbox.modules.releases.search import SearchOutcome, dedupe, search
from fetchbox.modules.releases.targets import movie_target, series_target
from fetchbox.modules.sources.service import SourcesService
from fetchbox.modules.system.config import ConfigService
from fetchbox.providers.indexers import SearchQuery

Mode = Literal["interactive", "automatic"]


def _daily(title: Title) -> bool:
    """Daily shows (talk shows, news) are searched by air date. TMDB has no flag for it;
    the genre is the hint."""
    return any(g in ("Talk", "News") for g in (title.meta or {}).get("genres") or [])


CACHE_TTL = 3600.0
CACHE_SIZE = 5000


@dataclass(frozen=True)
class CachedRelease:
    release_id: str
    wanted_item_id: str  # the first item it covers (a movie's only one)
    decision: Decision
    expires: float
    wanted_item_ids: tuple[str, ...] = ()  # every item it covers (a season pack: many)


@dataclass(frozen=True)
class InteractiveResult:
    wanted_item_id: str | None  # the episode or movie searched; None for a season search
    indexers: int
    outcome: SearchOutcome
    release_ids: list[str]  # parallel to outcome.decisions
    title_id: str = ""
    season: int | None = None


@dataclass
class Plan:
    """One search: what it's for, the query, how to judge results, and which wanted item
    each covered episode is (a movie: its one item under the key (0, 0))."""

    title_id: str
    target: Target
    query: SearchQuery
    ctx: DecisionContext
    items: dict[EpisodeKey, str]

    def covered(self, d: Decision) -> list[str]:
        if self.target.kind in ("movie", "music"):
            return list(self.items.values())
        return [self.items[e] for e in d.episodes if e in self.items]


class ReleaseCache:
    def __init__(self, size: int = CACHE_SIZE) -> None:
        self.size = size
        self._data: OrderedDict[str, CachedRelease] = OrderedDict()

    def put(self, wanted_item_ids: list[str], decision: Decision) -> str:
        rid = new_id()
        self._data[rid] = CachedRelease(
            rid,
            wanted_item_ids[0] if wanted_item_ids else "",
            decision,
            time.monotonic() + CACHE_TTL,
            tuple(wanted_item_ids),
        )
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
        # What the download clients take (set by the app): a Usenet release with no Usenet
        # client is rejected, saying so, instead of grabbed and stuck
        self.protocols: Callable[[], Awaitable[frozenset[str]]] | None = None

    async def plan(self, items: list[WantedItem], *, season_query: bool = False) -> Plan:
        """Build a search for some wanted items of one title: a movie, one episode, or
        episodes of one season (``season_query``: ask indexers for the season, so packs
        come back too)."""
        if not items:
            raise ValueError("nothing to search for")
        title = await self.media.get_title(items[0].title_id)
        ids = {"tmdb": str(title.tmdb_id)} if title.tmdb_id else {}
        if title.imdb_id:
            ids["imdb"] = title.imdb_id
        by_key: dict[EpisodeKey, str] = {}
        if title.kind == "music":
            w = items[0]
            target = await self._album_target(title, w)
            query = SearchQuery("music", (f"{title.name} {target.album}",))
            by_key[(0, 0)] = w.id
        elif title.kind == "movie":
            w = items[0]
            # Only an acquired item is compared with its file: after a redownload the item
            # is missing and the kept file is just replaced.
            existing = await self.existing(w.id) if w.state == "acquired" else None
            target = movie_target(title, existing)
            query = SearchQuery("movie", (title.name,), year=title.year, ids=ids)
            by_key[(0, 0)] = w.id
        else:
            if title.tvdb_id:
                ids["tvdb"] = str(title.tvdb_id)
            rows = await self.media.episode_items(title.id, item_ids=[w.id for w in items])
            wanted_eps = []
            for season, ep, w in rows:
                existing = await self.existing(w.id) if w.state == "acquired" else None
                wanted_eps.append(
                    WantedEpisode(
                        season, ep.number, ep.absolute_number, ep.air_date, existing=existing
                    )
                )
                by_key[(season, ep.number)] = w.id
            if not wanted_eps:
                raise ProblemError(
                    422, "Validation Failed", "no episodes to search for", slug="validation"
                )
            seasons = await self.media.season_info(title.id)
            target = series_target(title, wanted_eps, seasons)
            first = wanted_eps[0]
            single = len(wanted_eps) == 1 and not season_query
            query = SearchQuery(
                "series",
                (title.name,),
                year=title.year,
                season=first.season,
                episode=first.episode if single else None,
                air_date=first.air_date if single and _daily(title) else None,
                ids=ids,
            )
        profile = to_spec(await self.profiles.get_profile(title.profile_id))
        cfg = self.config.current.search
        async with self.db.sessionmaker() as s:
            blocked = await blocklist_keys(s, title.id)
            grabbed = [k for w in items for k in await grabbed_keys(s, w.id)]
        ctx = DecisionContext(
            profile=profile,
            formats=await self.profiles.format_specs(),
            blocklist=blocked,
            grabbed=grabbed,
            min_seeders=cfg.min_seeders,
            season_pack_share=cfg.season_pack_share,
            protocols=await self.protocols() if self.protocols else DecisionContext.protocols,
        )
        return Plan(title.id, target, query, ctx, by_key)

    async def _album_target(self, title: Title, w: WantedItem) -> Target:
        """An album: its name, the artist's other titles (to tell albums apart), and the
        wanted edition's length and track count (to judge sizes)."""
        from fetchbox.modules.media.models import Album, Track

        async with self.db.sessionmaker() as s:
            album = await s.get(Album, w.album_id) if w.album_id else None
            if album is None:
                raise ProblemError(422, "Validation Failed", "no album to search for",
                                   slug="validation")  # fmt: skip
            others = list(
                (
                    await s.execute(
                        select(Album.name).where(
                            Album.title_id == title.id,
                            Album.id != album.id,
                            Album.kind.in_(("album", "ep", "compilation", "live")),
                        )
                    )
                ).scalars()
            )
            lengths = list(
                (
                    await s.execute(select(Track.length_ms).where(Track.album_id == album.id))
                ).scalars()
            )
        existing = await self.existing(w.id) if w.state == "acquired" else None
        return Target(
            "music",
            (title.name,),
            year=album.release_date.year if album.release_date else None,
            existing=existing,
            album=album.name,
            album_kind=album.kind,
            other_albums=tuple(others),
            album_ms=sum(x or 0 for x in lengths) or None,
            album_tracks=len(lengths) or None,
        )

    async def existing(self, wanted_item_id: str) -> Existing | None:
        """The item's current file, as the decision engine compares upgrades against it."""
        q = (
            select(MediaFile)
            .join(MediaFileItem, MediaFileItem.media_file_id == MediaFile.id)
            .where(MediaFileItem.wanted_item_id == wanted_item_id)
            .order_by(MediaFile.imported_at.desc())
        )
        async with self.db.sessionmaker() as s:
            f = (await s.execute(q)).scalars().first()
        if f is None or not (f.quality or {}).get("name"):
            return None
        return Existing(
            quality=f.quality["name"],
            format_score=f.custom_format_score,
            revision=int(f.quality.get("revision") or 1),
        )

    async def run(
        self, items: list[WantedItem], *, mode: Mode, actor: Actor, season_query: bool = False
    ) -> tuple[int, SearchOutcome, Plan]:
        """Search every enabled indexer. Returns (indexers asked, outcome, the plan)."""
        plan = await self.plan(items, season_query=season_query)
        sources = await self.sources.release_sources()
        q = plan.query
        what = plan.target.names[0]
        if q.kind == "series" and q.season is not None:
            what += f" S{q.season:02d}" + (f"E{q.episode:02d}" if q.episode is not None else "")
        subject = (
            Subject(type="wanted_item", id=items[0].id)
            if len(items) == 1
            else Subject(type="title", id=plan.title_id)
        )
        await self.bus.emit(
            "search.started", subject=subject, actor=actor, name=what,
            mode=mode, indexers=len(sources),
        )  # fmt: skip
        outcome = await search(sources, q, plan.target, plan.ctx)
        now = utcnow()
        for w in items:
            await self.media.mark_searched(w.id, now)
        await self.bus.emit(
            "search.completed",
            subject=subject,
            actor=actor,
            name=what,
            mode=mode,
            indexers=len(sources),
            results=len(outcome.decisions),
            accepted=sum(d.accepted for d in outcome.decisions),
            failed_indexers=[f.source_name for f in outcome.failures],
        )
        return len(sources), outcome, plan

    async def evaluate(
        self, items: list[WantedItem], candidates: list[Candidate]
    ) -> tuple[SearchOutcome, Plan]:
        """Decide on releases found some other way (an RSS feed) as a search would."""
        plan = await self.plan(items, season_query=True)
        decisions = decide_all(dedupe(candidates), plan.target, plan.ctx)
        return SearchOutcome(decisions, pick_releases(decisions, plan.target), []), plan

    def _cache(self, plan: Plan, outcome: SearchOutcome) -> list[str]:
        return [self.cache.put(plan.covered(d), d) for d in outcome.decisions]

    async def interactive(self, wanted_item_id: str, *, actor: Actor) -> InteractiveResult:
        """One movie or one episode."""
        wanted = await self.media.get_wanted(wanted_item_id)
        n, outcome, plan = await self.run([wanted], mode="interactive", actor=actor)
        return InteractiveResult(wanted.id, n, outcome, self._cache(plan, outcome), plan.title_id)

    async def interactive_season(
        self, title_id: str, season: int, *, actor: Actor
    ) -> InteractiveResult:
        """A whole season: packs and single episodes, judged against every episode of it
        that isn't on disk yet (or all of them when every one is)."""
        title = await self.media.get_title(title_id)
        if title.kind != "series":
            raise ProblemError(409, "Conflict", "only series have seasons", slug="conflict")
        rows = await self.media.episode_items(title_id, season=season)
        if not rows:
            raise NotFoundError("season", f"{title_id}/{season}")
        items = [w for _, _, w in rows if w.state != "acquired"] or [w for _, _, w in rows]
        n, outcome, plan = await self.run(items, mode="interactive", actor=actor, season_query=True)
        return InteractiveResult(None, n, outcome, self._cache(plan, outcome), title_id, season)

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
        ids = wanted_item_ids or list(hit.wanted_item_ids) or [hit.wanted_item_id]
        return await self.grabs.grab(d, ids, reason=reason, actor=actor)
