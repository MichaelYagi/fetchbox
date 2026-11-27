"""Titles of plugin media kinds (a comic series): found with the kind's search, added with
its title() as items (issues) that are wanted and downloaded like episodes.

Movies, TV and music keep their own code; this is the path for kinds plugins add.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select

from fetchbox.core import kinds
from fetchbox.core.errors import ConflictError, NotFoundError, ProblemError
from fetchbox.core.events import Actor
from fetchbox.modules.media.models import PluginItem, Title, WantedItem
from fetchbox.modules.profiles.models import QualityProfile
from fetchbox.modules.storage.models import Destination, StorageNode
from fetchbox.plugin.host import Loaded, PluginHost
from fetchbox_plugin import Hit, MediaKind, TitleDetails


def _groups(details: TitleDetails) -> list[dict[str, Any]]:
    """The middle level (volumes), kept with the title: a release of a whole one is its items."""
    return [{"ref": g.ref, "number": g.number, "name": g.name} for g in details.groups]


LOOKUP_SECONDS = 30  # a kind's search or title() that takes longer is stopped
MONITOR = ("all", "future", "none")


class PluginTitles:
    def __init__(self, host: PluginHost, storage: Any) -> None:
        self.host = host
        self.storage = storage
        self.media = host.services.media
        self.db = self.media.db

    # ---- the kinds plugins add -------------------------------------------------------------

    def running(self) -> list[tuple[str, MediaKind, Loaded]]:
        return [(kid, k, got) for got in self.host.loaded.values() for kid, k in got.kinds.items()]

    def kind(self, kind_id: str) -> tuple[MediaKind, Loaded]:
        for kid, k, got in self.running():
            if kid == kind_id:
                return k, got
        raise NotFoundError("media kind", kind_id)

    async def search(self, kind_id: str, text: str) -> list[Hit]:
        k, got = self.kind(kind_id)
        try:
            async with asyncio.timeout(LOOKUP_SECONDS):
                return list(await k.search(got.ctx, text))
        except TimeoutError:
            raise ProblemError(
                504, "Timeout", f"{k.name} didn't answer in {LOOKUP_SECONDS} s"
            ) from None
        except Exception as exc:  # the plugin's failure, said as it said it
            raise ProblemError(
                502, "Bad Gateway", f"{k.name}: {exc}", slug="plugin-failed"
            ) from None

    async def _details(self, kind_id: str, ref: str) -> TitleDetails:
        k, got = self.kind(kind_id)
        try:
            async with asyncio.timeout(LOOKUP_SECONDS):
                return await k.title(got.ctx, ref)
        except TimeoutError:
            raise ProblemError(
                504, "Timeout", f"{k.name} didn't answer in {LOOKUP_SECONDS} s"
            ) from None
        except Exception as exc:
            raise ProblemError(
                502, "Bad Gateway", f"{k.name}: {exc}", slug="plugin-failed"
            ) from None

    # ---- adding and refreshing ---------------------------------------------------------------

    async def add(self, kind_id: str, ref: str, *, monitor: str = "all", actor: Actor) -> Title:
        if monitor not in MONITOR:
            raise ProblemError(
                422,
                "Validation Failed",
                f"monitor is one of {', '.join(MONITOR)}",
                slug="validation",
            )
        k, got = self.kind(kind_id)
        if await self._existing(kind_id, ref) is not None:
            raise ConflictError(f"{k.name}: that title is already in the library")
        details = await self._details(kind_id, ref)
        await self.folder(kind_id)
        async with self.db.sessionmaker() as s:
            profile = await s.scalar(select(QualityProfile.id).limit(1))
            title = Title(
                kind=kind_id, name=details.name or ref, sort_name=(details.name or ref).lower(),
                year=details.year, profile_id=profile, monitor_mode=monitor,
                meta={"ref": ref, "plugin": got.ctx.plugin_id, "overview": details.overview,
                      "poster_url": details.poster_url, "extra": details.extra,
                      "groups": _groups(details)},
            )  # fmt: skip
            s.add(title)
            await s.flush()
            await self._items(s, title, details, monitor)
            await s.commit()
            await s.refresh(title)
        await self.media._emit("title.added", title, actor)
        return title

    async def refresh(self, title_id: str, *, actor: Actor) -> Title:
        title = await self.media.get_title(title_id)
        ref = (title.meta or {}).get("ref")
        if not ref or kinds.find(title.kind) is None or kinds.get(title.kind).core:
            raise ConflictError("not a plugin media kind's title (or its plugin isn't running)")
        details = await self._details(title.kind, ref)
        async with self.db.sessionmaker() as s:
            row = await s.get(Title, title_id)
            assert row is not None
            row.name, row.year = details.name or row.name, details.year or row.year
            row.meta = {**(row.meta or {}), "overview": details.overview,
                        "poster_url": details.poster_url, "extra": details.extra,
                        "groups": _groups(details)}  # fmt: skip
            await self._items(s, row, details, row.monitor_mode)
            await s.commit()
            await s.refresh(row)
        await self.media._emit("title.updated", row, actor)
        return row

    async def _items(self, s: Any, title: Title, details: TitleDetails, monitor: str) -> None:
        have = {
            p.ref: p
            for p in (
                await s.execute(select(PluginItem).where(PluginItem.title_id == title.id))
            ).scalars()
        }
        today = date.today()
        for it in details.items:
            row = have.get(it.ref)
            if row is None:
                row = PluginItem(title_id=title.id, ref=it.ref)
                s.add(row)
                await s.flush()
                wanted = monitor == "all" or (
                    monitor == "future" and (it.released is None or it.released > today)
                )
                s.add(WantedItem(title_id=title.id, plugin_item_id=row.id, monitored=wanted))
            row.number, row.name, row.released, row.group_ref = (
                it.number,
                it.name,
                it.released,
                it.group,
            )

    async def _existing(self, kind_id: str, ref: str) -> Title | None:
        async with self.db.sessionmaker() as s:
            for t in (await s.execute(select(Title).where(Title.kind == kind_id))).scalars():
                if (t.meta or {}).get("ref") == ref:
                    return t
        return None

    async def items(self, title_id: str) -> list[dict[str, Any]]:
        async with self.db.sessionmaker() as s:
            rows = (
                await s.execute(
                    select(PluginItem, WantedItem)
                    .join(WantedItem, WantedItem.plugin_item_id == PluginItem.id)
                    .where(PluginItem.title_id == title_id)
                )
            ).all()
        from fetchbox.modules.library.models import MediaFile, MediaFileItem

        async with self.db.sessionmaker() as s:
            files = {
                w: (q or {}).get("name")
                for w, q in (
                    await s.execute(
                        select(MediaFileItem.wanted_item_id, MediaFile.quality)
                        .join(MediaFile, MediaFile.id == MediaFileItem.media_file_id)
                        .where(MediaFileItem.wanted_item_id.in_([w.id for _p, w in rows]))
                    )
                ).all()
            }
        out = [
            {"id": w.id, "ref": p.ref, "number": p.number, "name": p.name, "released": p.released,
             "state": w.state, "monitored": w.monitored, "version": w.version,
             "quality": files.get(w.id)}
            for p, w in rows
        ]  # fmt: skip
        out.sort(key=lambda x: (x["number"] is None, x["number"] or 0, x["released"] or date.min))
        return out

    # ---- the kind's library folder ------------------------------------------------------------

    async def folder(self, kind_id: str) -> Destination:
        """The kind's default library folder: made the first time (beside Movies, named after
        the kind), then whatever Settings says."""
        found = await self.storage.destinations(kind_id)
        if found:
            return found[0]  # type: ignore[no-any-return]
        k = kinds.get(kind_id)
        async with self.db.sessionmaker() as s:
            node = await s.scalar(select(StorageNode).where(StorageNode.kind == "local"))
            movies = await s.scalar(
                select(Destination).where(
                    Destination.media_kind == "movie", Destination.is_default.is_(True)
                )
            )
            base = Path(movies.path).parent if movies else Path(self.storage.downloads_root)
            taken = set((await s.execute(select(Destination.name))).scalars())
            name, n = k.plural, 1
            while name in taken:
                n += 1
                name = f"{k.plural} {n}"
            dest = Destination(node_id=node.id if node else None, name=name, media_kind=kind_id,
                               path=str(base / k.plural), is_default=True)  # fmt: skip
            s.add(dest)
            await s.commit()
            await s.refresh(dest)
        await asyncio.to_thread(Path(dest.path).mkdir, parents=True, exist_ok=True)
        return dest
