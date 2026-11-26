"""The parts of ``ctx`` that work on the library: ``files``, ``library``, ``jobs`` and
``notify``. Each goes through Fetchbox's own services, so a plugin is held to the same rules
and sends the same events as the web UI."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from fetchbox.core.db import utcnow
from fetchbox.core.errors import NotFoundError
from fetchbox.core.events import Actor
from fetchbox.core.jobs import JobContext, JobRunner
from fetchbox.modules.library.models import MediaFile, MediaFileSidecar
from fetchbox.modules.library.sidecars import sidecar_rel
from fetchbox.modules.media.models import Title
from fetchbox.plugin.types import FileInfo, SidecarInfo, TitleInfo

if TYPE_CHECKING:
    from fetchbox.modules.automation.search import AutoSearch
    from fetchbox.modules.library.service import LibraryService
    from fetchbox.modules.media.service import MediaService
    from fetchbox.modules.notifications.service import NotificationsService

PLUGIN_MESSAGE = "plugin.message"  # the notification event a plugin's messages go out as


class PluginRefused(PermissionError):
    """Something a plugin may not do (another plugin's sidecar, say)."""


@dataclass
class Services:
    """Fetchbox's services the plugin context works through (made once, in the app)."""

    library: LibraryService
    media: MediaService
    jobs: JobRunner
    notifications: NotificationsService
    autosearch: AutoSearch | None = None


def _actor(plugin_id: str) -> Actor:
    """Who did it, in history and the activity log: the plugin (a system actor, so the
    API's actor types are unchanged)."""
    return Actor(type="system", id=f"plugin:{plugin_id}", name=f"{plugin_id} plugin")


def _title(t: Title, *, monitored: bool) -> TitleInfo:
    ids = {k: v for k in ("tmdb_id", "tvdb_id", "imdb_id", "mbid") if (v := getattr(t, k))}
    return TitleInfo(t.id, t.kind, t.name, t.year, ids, monitored, t.folder)


class PluginFiles:
    """``ctx.files``: a library file's place on disk, and sidecar files tied to it."""

    def __init__(self, plugin_id: str, services: Services) -> None:
        self.plugin_id = plugin_id
        self._lib = services.library

    async def _where(self, f: MediaFile) -> Path:
        if not f.destination_id:
            raise NotFoundError("file in a library folder", f.id)
        dest = await self._lib.storage.destination(f.destination_id)
        return Path(dest.path)

    async def path(self, file_id: str) -> Path:
        f, _ = await self._lib.file(file_id)
        return await self._where(f) / f.relative_path

    async def _info(self, row: MediaFileSidecar) -> SidecarInfo:
        f, _ = await self._lib.file(row.media_file_id)
        where = await self._where(f) / sidecar_rel(f.relative_path, row.suffix)
        return SidecarInfo(row.media_file_id, row.suffix, where, row.plugin, dict(row.meta or {}))

    async def sidecars(self, file_id: str, *, suffix: str | None = None) -> list[SidecarInfo]:
        """The file's sidecars (every plugin's); ``suffix=".srt"`` keeps those ending so."""
        rows = await self._lib.sidecars(file_id)
        return [await self._info(r) for r in rows if suffix is None or r.suffix.endswith(suffix)]

    async def add_sidecar(
        self, file_id: str, suffix: str, data: bytes, *, meta: dict[str, Any] | None = None
    ) -> SidecarInfo:
        """Write ``data`` beside the file as ``<its name>.<suffix>``: from then on it's
        renamed, moved, recycled and deleted with the file."""
        mine = await self._owner(file_id, suffix)
        if mine not in (None, self.plugin_id):
            raise PluginRefused(f"{suffix} on file {file_id} is the {mine} plugin's")
        row = await self._lib.add_sidecar(file_id, suffix, data, plugin=self.plugin_id, meta=meta)
        return await self._info(row)

    async def remove_sidecar(self, file_id: str, suffix: str) -> bool:
        mine = await self._owner(file_id, suffix)
        if mine is None:
            return False
        if mine != self.plugin_id:
            raise PluginRefused(f"{suffix} on file {file_id} is the {mine} plugin's")
        return await self._lib.remove_sidecar(file_id, suffix)

    async def _owner(self, file_id: str, suffix: str) -> str | None:
        rows = await self._lib.sidecars(file_id)
        return next((r.plugin for r in rows if r.suffix == suffix), None)


class PluginLibrary:
    """``ctx.library``: read titles and their files; search for an item; mark it wanted."""

    def __init__(self, plugin_id: str, services: Services) -> None:
        self.plugin_id = plugin_id
        self._s = services

    async def titles(self, *, kind: str | None = None) -> list[TitleInfo]:
        db = self._s.media.db
        async with db.sessionmaker() as s:
            q = select(Title).order_by(Title.sort_name)
            if kind is not None:
                q = q.where(Title.kind == kind)
            rows = list((await s.execute(q)).scalars())
        return [_title(t, monitored=t.monitor_mode != "none") for t in rows]

    async def title(self, title_id: str) -> TitleInfo:
        t = await self._s.media.get_title(title_id)
        return _title(t, monitored=t.monitor_mode != "none")

    async def files(self, title_id: str) -> list[FileInfo]:
        rows = await self._s.library.files(title_id)
        return [await self._file(f, items, title_id) for f, items in rows]

    async def file(self, file_id: str) -> FileInfo:
        f, items = await self._s.library.file(file_id)
        title_id = None
        if items:
            title_id = (await self._s.media.get_wanted(items[0])).title_id
        return await self._file(f, items, title_id)

    async def _file(self, f: MediaFile, items: list[str], title_id: str | None) -> FileInfo:
        where = PluginFiles(self.plugin_id, self._s)
        quality = str((f.quality or {}).get("name") or "Unknown")
        path = await where.path(f.id)
        return FileInfo(f.id, title_id, path, f.size, quality, tuple(items), f.imported_at)

    async def wanted(self, item_id: str, monitored: bool = True) -> None:
        """Mark an item (a movie, an episode, an album) wanted, or not."""
        await self._s.media.update_wanted(
            item_id, monitored=monitored, if_match=None, actor=_actor(self.plugin_id)
        )

    async def search(self, item_id: str) -> str | None:
        """Search for an item now, as Search does on its page; the search job's id."""
        if self._s.autosearch is None:
            return None
        await self._s.media.get_wanted(item_id)  # NotFoundError for an unknown item
        job = await self._s.autosearch.queued(item_id, reason="manual")
        return job.id if job else None


Handler = Callable[[JobContext], Awaitable[dict[str, Any] | None]]


class PluginJobs:
    """``ctx.jobs``: the plugin's background jobs. They show in the job list, are retried,
    and survive a restart. ``@ctx.jobs.job("sync")`` declares one; ``enqueue("sync")``
    queues it. Each runs with a JobContext: ``payload``, ``progress()``, ``check_cancelled()``."""

    def __init__(self, plugin_id: str, runner: JobRunner) -> None:
        self.plugin_id = plugin_id
        self._runner = runner

    def type_of(self, name: str) -> str:
        return f"plugin.{self.plugin_id}.{name}"

    def job(self, name: str, *, max_attempts: int = 3) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            async def run(job: JobContext) -> dict[str, Any]:
                return dict(await fn(job) or {})

            self._runner.register(self.type_of(name), run, queue="io", max_attempts=max_attempts)
            return fn

        return deco

    async def enqueue(
        self, name: str, payload: dict[str, Any] | None = None, *, dedupe_key: str | None = None
    ) -> str | None:
        """Queue the job; None when the same ``dedupe_key`` is already queued or running."""
        key = f"{self.type_of(name)}:{dedupe_key}" if dedupe_key else None
        job = await self._runner.enqueue(self.type_of(name), payload, dedupe_key=key)
        return job.id if job else None


class PluginNotify:
    """``ctx.notify(title, body)``: a message through the notification targets (Discord,
    webhooks) that chose plugin messages in Settings."""

    def __init__(self, plugin_id: str, notifications: NotificationsService) -> None:
        self.plugin_id = plugin_id
        self._n = notifications

    async def __call__(self, title: str, body: str = "", *, level: str = "info") -> int:
        """How many targets it went to."""
        from fetchbox.providers.notifiers import Notification

        n = Notification.model_validate(
            {"event": PLUGIN_MESSAGE, "title": title, "body": body, "level": level,
             "occurred_at": utcnow(), "data": {"plugin": self.plugin_id}}
        )  # fmt: skip
        return await self._n.send(n)


__all__ = [
    "PLUGIN_MESSAGE",
    "PluginFiles",
    "PluginJobs",
    "PluginLibrary",
    "PluginNotify",
    "PluginRefused",
    "Services",
]
