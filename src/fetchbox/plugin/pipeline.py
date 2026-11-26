"""The download pipeline for plugins' media kinds: search, decide, grab, and import.

The kind answers what's specific to it (the queries to ask, what a release name says,
which file is which item, the naming); Fetchbox does the rest as for every title: the
indexers, the blocklist, grabs, the download clients, placing files, history and events.
Movies, TV and music keep their own pipeline; this one runs only for plugin kinds.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

import structlog
from sqlalchemy import select

from fetchbox.core.events import Actor
from fetchbox.modules.library.naming import safe_component
from fetchbox.modules.library.service import ImportProblem
from fetchbox.modules.media.models import PluginItem, Title, WantedItem
from fetchbox.modules.releases.decision import Decision, title_key
from fetchbox.modules.releases.parser import parse_release
from fetchbox.plugin.titles import LOOKUP_SECONDS, PluginTitles
from fetchbox.providers.indexers import ReleaseCandidate, SearchQuery
from fetchbox_plugin import File, Item, MediaKind, TitleDetails

log = structlog.get_logger(__name__)

SEARCH_MISSING = "plugins.search_missing"  # the schedule (and job) that searches them all


@dataclass
class Pick:
    candidate: ReleaseCandidate
    quality: str
    rank: int  # the kind's quality order: 0 is the best
    items: list[str]  # the wanted items it covers


class PluginPipeline:
    def __init__(self, titles: PluginTitles, *, sources: Any, grabs: Any, library: Any) -> None:
        self.titles = titles
        self.sources = sources
        self.grabs = grabs
        self.library = library
        self.db = titles.db

    async def _rows(self, title_id: str) -> list[tuple[PluginItem, WantedItem]]:
        async with self.db.sessionmaker() as s:
            return [
                (p, w)
                for p, w in (
                    await s.execute(
                        select(PluginItem, WantedItem)
                        .join(WantedItem, WantedItem.plugin_item_id == PluginItem.id)
                        .where(PluginItem.title_id == title_id)
                    )
                ).all()
            ]

    @staticmethod
    def _details(title: Title, rows: list[tuple[PluginItem, WantedItem]]) -> TitleDetails:
        meta = title.meta or {}
        items = [Item(p.ref, p.number, p.name, p.released, p.group_ref) for p, _w in rows]
        return TitleDetails(meta.get("ref") or title.id, title.name, title.year,
                            meta.get("overview") or "", meta.get("poster_url"), items=items,
                            extra=dict(meta.get("extra") or {}))  # fmt: skip

    # ---- search, decide, grab --------------------------------------------------------------

    async def search(
        self, title_id: str, item_ids: list[str] | None = None, *, actor: Actor
    ) -> dict[str, Any]:
        """Search for a plugin kind's title's wanted items (all that are out and missing, or
        those given), and grab the best releases."""
        title = await self.titles.media.get_title(title_id)
        kind, _got = self.titles.kind(title.kind)
        rows = await self._rows(title_id)
        today = date.today()
        wanted = [
            (p, w) for p, w in rows
            if w.state == "missing" and (w.id in item_ids if item_ids else w.monitored
                                         and (p.released is None or p.released <= today))
        ]  # fmt: skip
        if not wanted:
            return {"searched": 0, "results": 0, "grabbed": 0}
        details = self._details(title, rows)
        items = [Item(p.ref, p.number, p.name, p.released, p.group_ref) for p, _w in wanted]
        queries = kind.queries(details, items)
        sources = await self.sources.release_sources()
        found: dict[str, ReleaseCandidate] = {}
        for q in queries:
            sq = SearchQuery(
                kind=title.kind, terms=(q.text,), categories=q.categories or kind.categories
            )
            for src in sources:
                try:
                    async with asyncio.timeout(LOOKUP_SECONDS):
                        for c in await src.search(sq):
                            found.setdefault(c.info_hash or c.guid or c.title, c)
                except Exception as exc:  # one indexer failing never stops the search
                    log.warning(
                        "plugin.search_indexer_failed",
                        indexer=getattr(src, "name", ""),
                        error=str(exc),
                    )
        picks = await self.decide(title, kind, details, wanted, list(found.values()))
        grabbed = 0
        needed = {w.id for _p, w in wanted}
        for pick in picks:
            if not set(pick.items) <= needed:
                continue  # already covered by a better release
            decision = Decision(
                candidate=pick.candidate, parsed=parse_release(pick.candidate.title),
                quality=pick.quality, tier=None, format_score=0, matched_formats=[],
                cutoff_met=False,
            )  # fmt: skip
            try:
                _grab, created = await self.grabs.grab(
                    decision, pick.items, reason="search", actor=actor
                )
            except Exception as exc:  # taken meanwhile, gone: the next one
                log.info("plugin.grab_skipped", release=pick.candidate.title, why=str(exc))
                continue
            if created:
                grabbed += 1
                needed -= set(pick.items)
            if not needed:
                break
        return {"searched": len(queries), "results": len(found), "grabbed": grabbed}

    async def decide(
        self,
        title: Title,
        kind: MediaKind,
        details: TitleDetails,
        wanted: list[tuple[PluginItem, WantedItem]],
        found: list[ReleaseCandidate],
    ) -> list[Pick]:
        """The releases worth grabbing, best first: this title's, of wanted items, not
        blocklisted; the kind's better quality, then more wanted items, then more seeders."""
        by_number = {p.number: w.id for p, w in wanted if p.number is not None}
        entries = await self.grabs.blocklist(title.id)
        blocked = {(e.release_title or "").lower() for e in entries}
        blocked |= {e.info_hash for e in entries if e.info_hash}
        want_key = title_key(details.name)
        names = list(kind.qualities.names)
        picks = []
        for c in found:
            if c.title.lower() in blocked or (c.info_hash and c.info_hash in blocked):
                continue
            try:
                parsed = kind.parse(c.title)
            except Exception:  # the plugin's parser failing on a name: not this kind's
                continue
            if parsed is None or title_key(parsed.title) != want_key:
                continue
            if (
                parsed.year
                and details.year
                and abs(parsed.year - details.year) > 1
                and len(parsed.numbers) != 1
            ):
                continue
            covered = [by_number[n] for n in parsed.numbers if n in by_number]
            if not covered:
                continue
            quality = parsed.quality if parsed.quality in names else None
            rank = names.index(quality) if quality else len(names)
            picks.append(Pick(c, quality or "Unknown", rank, covered))
        picks.sort(key=lambda p: (p.rank, -len(p.items), -(p.candidate.seeders or 0)))
        return picks

    # ---- import ----------------------------------------------------------------------------

    async def import_download(
        self, d: Any, grab: Any, title: Title, waiting: list[WantedItem], dest: Any
    ) -> dict[str, Any]:
        """A finished download of a plugin kind's title: the kind says which file is which
        item; each is named by the kind's naming and filed like any other."""
        lib = self.library
        try:
            kind, _got = self.titles.kind(title.kind)
        except Exception:
            raise ImportProblem(f"the plugin for {title.kind} isn't running") from None
        rows = await self._rows(title.id)
        by_wanted = {w.id: p for p, w in rows}
        items = [
            Item(by_wanted[w.id].ref, by_wanted[w.id].number, by_wanted[w.id].name,
                 by_wanted[w.id].released, by_wanted[w.id].group_ref)
            for w in waiting if w.id in by_wanted
        ]  # fmt: skip
        ref_to_wanted = {by_wanted[w.id].ref: w for w in waiting if w.id in by_wanted}
        client_files = {f.path.replace("\\", "/"): f for f in await lib._files(d)}
        files = [File(path, f.size) for path, f in client_files.items()]
        try:
            chosen = kind.pick(files, items)
        except Exception as exc:
            raise ImportProblem(
                f"{kind.name} couldn't tell the files apart: {exc}", retry=False
            ) from None
        if not chosen:
            raise ImportProblem(
                f"no file in the download is one of the wanted {kind.levels[-1]}s", retry=False
            )
        details = self._details(title, rows)
        quality = (grab.release or {}).get("quality") or "Unknown"
        parsed = parse_release(grab.release_title)
        done = []
        for path, ref in chosen.items():
            cf = client_files.get(path.replace("\\", "/"))
            w = ref_to_wanted.get(ref)
            if cf is None or w is None:
                continue
            item = next(i for i in items if i.ref == ref)
            try:
                rel = kind.naming.pattern.format(**kind.name_tokens(details, item))
            except Exception as exc:
                raise ImportProblem(f"{kind.name}'s naming failed: {exc}", retry=False) from None
            parts = [
                safe_component(x) for x in PurePosixPath(rel).parts if x not in ("", ".", "..")
            ]
            rel = "/".join(parts) + PurePosixPath(cf.path).suffix.lower()
            src = await lib._on_disk(d, cf)
            dst = Path(dest.path) / rel
            mode = await lib._place(src, dst, dest, d, cf.path)
            mf, replaced, _cutoff = await lib._record(
                d, grab, title, dest, [w], rel, dst, mode, parsed, quality, 0, None, cf.path
            )
            await lib._announce(title, dest, mf, rel, quality, mode, replaced)
            done.append({"media_file_id": mf.id, "path": str(dst), "link_mode": mode})
        if not done:
            raise ImportProblem("the plugin chose no file of the download", retry=False)
        return {"files": done}

    # ---- every few hours ---------------------------------------------------------------------

    async def search_missing(self, *, actor: Actor) -> dict[str, Any]:
        """Every plugin kind's titles with wanted items out and missing: searched."""
        running = {kid for kid, _k, _g in self.titles.running()}
        if not running:
            return {"titles": 0, "grabbed": 0}
        async with self.db.sessionmaker() as s:
            ids = list((await s.execute(select(Title.id).where(Title.kind.in_(running)))).scalars())
        grabbed = 0
        for tid in ids:
            try:
                grabbed += (await self.search(tid, actor=actor))["grabbed"]
            except Exception as exc:
                log.warning("plugin.search_missing_failed", title=tid, error=str(exc))
        return {"titles": len(ids), "grabbed": grabbed}
