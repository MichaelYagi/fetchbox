"""Public events: the stable view of what happens in Fetchbox that plugins react to (the
types themselves are in the plugin API package, fetchbox_plugin.events).

Fetchbox's own events are internal (names and data may change); plugins see these instead,
translated from them here. Nothing is added to the bus for this, so the Activity log,
notifications, webhooks and the event stream are exactly as they were.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from fetchbox.core.events import Event, EventBus
from fetchbox_plugin.events import PUBLIC_EVENTS, PublicEvent


def _pick(*keys: str) -> Callable[[Event], dict[str, Any]]:
    return lambda ev: {k: ev.data[k] for k in keys if k in ev.data}


def _subject(ev: Event, kind: str) -> str | None:
    return ev.subject.id if ev.subject and ev.subject.type == kind else None


# internal type -> (public type, its data)
_MAP: dict[str, tuple[str, Callable[[Event], dict[str, Any]]]] = {
    "title.added": ("title.added", _pick("name", "year")),
    "title.updated": ("title.updated", _pick("name", "year", "fields")),
    "title.removed": ("title.removed", _pick("name", "year")),
    "release.grabbed": (
        "release.grabbed",
        _pick("release_title", "indexer", "quality", "reason", "wanted_item_ids"),
    ),
    "release.blocklisted": ("release.blocklisted", _pick("release_title", "reason")),
    "download.completed": ("download.completed", _pick("name", "save_path")),
    "download.failed": ("download.failed", _pick("name", "error")),
    "import.completed": ("file.imported", _pick("path", "quality", "destination_id")),
    "file.upgraded": ("file.upgraded", _pick("path", "replaced")),
    "file.renamed": ("file.renamed", _pick("old", "new", "destination_id")),
    "file.deleted": ("file.deleted", _pick("path", "from_disk")),
    "library.folder_changed": ("library.folder_changed", _pick("folders")),
    "system.started": ("system.started", _pick("version")),
    "system.stopping": ("system.stopping", _pick()),
}
INTERNAL_TYPES = tuple(_MAP)


def to_public(ev: Event) -> PublicEvent | None:
    """The public view of an internal event, or None when it isn't one plugins see."""
    hit = _MAP.get(ev.type)
    if hit is None:
        return None
    public, data = hit
    title_id = ev.data.get("title_id") or _subject(ev, "title")
    file_ids = list(ev.data.get("file_ids") or [])
    if not file_ids and (f := _subject(ev, "media_file")):
        file_ids = [f]
    return PublicEvent(
        id=ev.id,
        type=public,
        occurred_at=ev.occurred_at,
        title_id=title_id,
        kind=ev.data.get("kind"),
        file_ids=file_ids,
        data={k: v for k, v in data(ev).items() if v is not None},
    )


async def public_events(bus: EventBus, *types: str) -> AsyncIterator[PublicEvent]:
    """Public events as they happen (all, or of ``types``)."""
    wanted = set(types)
    unknown = wanted - set(PUBLIC_EVENTS)
    if unknown:
        raise ValueError(f"not public events: {', '.join(sorted(unknown))}")
    async for ev in bus.subscribe(*INTERNAL_TYPES):
        pub = to_public(ev)
        if pub is not None and (not wanted or pub.type in wanted):
            yield pub
