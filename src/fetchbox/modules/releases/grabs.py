"""Grabs and the blocklist.

A grab records the decision to download a release for wanted items: the release
snapshot, why, and who. Grabbing moves the items to ``grabbed`` and writes a
``grabbed`` history event per item. The same release (by info hash, else title) grabbed
again for the same item returns the existing grab instead of a duplicate.

Downloading starts when the downloads module lands; it will pick grabs up from the
``release.grabbed`` event.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fetchbox.core.db import Database
from fetchbox.core.errors import ConflictError, FieldError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.modules.media.models import HistoryEvent, Title, WantedItem
from fetchbox.modules.releases.decision import Decision, ReleaseKey
from fetchbox.modules.releases.models import BlocklistEntry, Grab, GrabItem

Reason = Literal["manual", "rss", "search", "upgrade"]
BUSY = ("grabbed", "downloading", "importing")


def _actor_name(actor: Actor) -> str:
    return actor.name or actor.id or actor.type


async def blocklist_keys(s: AsyncSession, title_id: str) -> list[ReleaseKey]:
    rows = (
        await s.execute(select(BlocklistEntry).where(BlocklistEntry.title_id == title_id))
    ).scalars()
    return [ReleaseKey(r.info_hash, r.guid, r.release_title) for r in rows]


async def last_unlock(s: AsyncSession, wanted_item_ids: Sequence[str]) -> datetime | None:
    """When any of the items was last unlocked: grabs before that don't count as
    "already grabbed" any more."""
    out: datetime | None = await s.scalar(
        select(func.max(HistoryEvent.created_at)).where(
            HistoryEvent.wanted_item_id.in_(wanted_item_ids), HistoryEvent.type == "unlocked"
        )
    )
    return out


async def grabbed_keys(s: AsyncSession, wanted_item_id: str) -> list[ReleaseKey]:
    """Releases already grabbed for the item, since it was last unlocked: a redownload
    without blocklisting may take the same release again."""
    q = (
        select(Grab)
        .join(GrabItem, GrabItem.grab_id == Grab.id)
        .where(GrabItem.wanted_item_id == wanted_item_id)
    )
    if (since := await last_unlock(s, [wanted_item_id])) is not None:
        q = q.where(Grab.created_at > since)
    rows = (await s.execute(q)).scalars()
    return [
        ReleaseKey(r.info_hash, r.release.get("candidate", {}).get("guid"), r.release_title)
        for r in rows
    ]


class GrabsService:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus

    # ---- grabs ------------------------------------------------------------------------------

    async def grab(
        self,
        decision: Decision,
        wanted_item_ids: Sequence[str],
        *,
        reason: Reason,
        actor: Actor,
        idempotency_key: str | None = None,
    ) -> tuple[Grab, bool]:
        """Returns (grab, created). ``created`` is False for a duplicate grab."""
        c = decision.candidate
        ids = list(dict.fromkeys(wanted_item_ids))
        async with self.db.sessionmaker() as s:
            items = list(
                (await s.execute(select(WantedItem).where(WantedItem.id.in_(ids)))).scalars()
            )
            if missing := sorted(set(ids) - {w.id for w in items}):
                raise NotFoundError("wanted item", ", ".join(missing))

            same = Grab.info_hash == c.info_hash if c.info_hash else Grab.release_title == c.title
            dup_q = (
                select(Grab)
                .join(GrabItem, GrabItem.grab_id == Grab.id)
                .where(same, GrabItem.wanted_item_id.in_(ids))
            )
            if (since := await last_unlock(s, ids)) is not None:
                dup_q = dup_q.where(Grab.created_at > since)
            if (dup := (await s.execute(dup_q)).scalars().first()) is not None:
                return dup, False

            if busy := [w for w in items if w.state in BUSY]:
                raise ConflictError(
                    f"already being downloaded ({busy[0].state}): wanted item {busy[0].id}; "
                    "remove that download first"
                )
            title = await s.get(Title, items[0].title_id)
            grab = Grab(
                release=decision.model_dump(mode="json"),
                indexer_id=c.source_id or None,
                info_hash=c.info_hash,
                release_title=c.title,
                reason=reason,
                score=decision.format_score,
                actor=_actor_name(actor),
                idempotency_key=idempotency_key,
            )
            s.add(grab)
            await s.flush()
            # Conditional update: of two concurrent grabs for an item, only one claims it.
            claimed = await s.execute(
                update(WantedItem)
                .where(WantedItem.id.in_(ids), WantedItem.state.not_in(BUSY))
                .values(state="grabbed", version=WantedItem.version + 1)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != len(ids):  # type: ignore[attr-defined]
                await s.rollback()
                raise ConflictError("another grab claimed these wanted items first; search again")
            for w in items:
                s.add(GrabItem(grab_id=grab.id, wanted_item_id=w.id))
                s.add(
                    HistoryEvent(
                        wanted_item_id=w.id,
                        title_id=w.title_id,
                        type="grabbed",
                        data={
                            "grab_id": grab.id,
                            "release_title": c.title,
                            "indexer": c.source_name,
                            "quality": decision.quality,
                            "format_score": decision.format_score,
                            "reason": reason,
                            "actor": grab.actor,
                        },
                    )
                )
            await s.commit()
        await self.bus.emit(
            "release.grabbed",
            subject=Subject(type="grab", id=grab.id),
            actor=actor,
            name=title.name if title else c.title,
            release_title=c.title,
            indexer=c.source_name,
            quality=decision.quality,
            reason=reason,
            wanted_item_ids=ids,
        )
        return grab, True

    async def get(self, grab_id: str) -> tuple[Grab, list[str]]:
        async with self.db.sessionmaker() as s:
            grab = await s.get(Grab, grab_id)
            if grab is None:
                raise NotFoundError("grab", grab_id)
            return grab, await self._items(s, grab_id)

    async def _items(self, s: AsyncSession, grab_id: str) -> list[str]:
        q = select(GrabItem.wanted_item_id).where(GrabItem.grab_id == grab_id)
        return list((await s.execute(q)).scalars())

    async def for_wanted(self, wanted_item_id: str) -> list[tuple[Grab, list[str]]]:
        async with self.db.sessionmaker() as s:
            q = (
                select(Grab)
                .join(GrabItem, GrabItem.grab_id == Grab.id)
                .where(GrabItem.wanted_item_id == wanted_item_id)
                .order_by(Grab.id.desc())
            )
            grabs = list((await s.execute(q)).scalars())
            return [(g, await self._items(s, g.id)) for g in grabs]

    # ---- blocklist --------------------------------------------------------------------------

    async def blocklist(self, title_id: str | None) -> list[BlocklistEntry]:
        async with self.db.sessionmaker() as s:
            q = select(BlocklistEntry).order_by(BlocklistEntry.id.desc())
            if title_id:
                q = q.where(BlocklistEntry.title_id == title_id)
            return list((await s.execute(q)).scalars())

    async def block(
        self,
        *,
        title_id: str | None,
        info_hash: str | None,
        guid: str | None,
        release_title: str | None,
        indexer_id: str | None,
        reason: str | None,
        actor: Actor,
    ) -> tuple[BlocklistEntry, bool]:
        """Returns (entry, created); blocking the same release twice is a no-op."""
        if not (info_hash or guid or release_title):
            raise ProblemError(
                422,
                "Validation Failed",
                slug="validation",
                errors=[FieldError(field="body", message="give info_hash, guid or release_title")],
            )
        info_hash = info_hash.lower() if info_hash else None
        async with self.db.sessionmaker() as s:
            if title_id and await s.get(Title, title_id) is None:
                raise ProblemError(
                    422,
                    "Validation Failed",
                    slug="validation",
                    errors=[FieldError(field="body.title_id", message="no such title")],
                )
            conds: list[Any] = []
            if info_hash:
                conds.append(BlocklistEntry.info_hash == info_hash)
            if guid:
                conds.append(BlocklistEntry.guid == guid)
            if not conds:
                conds.append(BlocklistEntry.release_title == release_title)
            q = select(BlocklistEntry).where(BlocklistEntry.title_id == title_id, or_(*conds))
            if (dup := (await s.execute(q)).scalars().first()) is not None:
                return dup, False
            entry = BlocklistEntry(
                title_id=title_id,
                info_hash=info_hash,
                guid=guid,
                release_title=release_title,
                indexer_id=indexer_id,
                reason=reason,
            )
            s.add(entry)
            await s.commit()
        await self.bus.emit(
            "release.blocklisted",
            subject=Subject(type="blocklist", id=entry.id),
            actor=actor,
            release_title=release_title or info_hash or guid,
            reason=reason,
        )
        return entry, True

    async def unblock(self, entry_id: str, *, actor: Actor) -> None:
        async with self.db.sessionmaker() as s:
            entry = await s.get(BlocklistEntry, entry_id)
            if entry is None:
                raise NotFoundError("blocklist entry", entry_id)
            await s.delete(entry)
            await s.commit()
        await self.bus.emit(
            "release.unblocklisted",
            subject=Subject(type="blocklist", id=entry_id),
            actor=actor,
            release_title=entry.release_title or entry.info_hash or entry.guid,
        )
