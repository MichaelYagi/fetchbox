"""Notifications: which events reach which target, and what the message says.

Routing lives here, not in the providers (spec): each target lists the events it wants;
an event turns into one short message (title, body, level) and is sent to every enabled
target that wants it, in the background, so a slow or broken webhook never holds up
imports or downloads. The last error is kept on the target and shown in Settings.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from fetchbox.core.concurrency import check_if_match
from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, FieldError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, Event, EventBus, Subject
from fetchbox.core.ids import new_id
from fetchbox.core.providers import ProviderRegistry, TestResult
from fetchbox.core.secrets import (
    SecretBox,
    mask_settings,
    merge_update,
    open_settings,
    seal_settings,
)
from fetchbox.modules.notifications.models import NotificationTarget
from fetchbox.providers.notifiers import Level, Notification, NotifierError, NotifierProvider

log = structlog.get_logger(__name__)


def _health_level(status: str) -> Level:
    return "success" if status == "ok" else "warning" if status == "warning" else "error"


def _year(d: dict[str, Any]) -> str:
    return f" ({d['year']})" if d.get("year") else ""


# event -> (label for Settings, on by default, message maker)
Maker = Callable[[dict[str, Any]], tuple[str, str, Level]]
EVENTS: dict[str, tuple[str, bool, Maker]] = {
    "import.completed": ("Downloaded and added to the library", True, lambda d: (
        f"Downloaded {d.get('name', '')}{_year(d)}",
        "\n".join(x for x in (d.get("path"), d.get("quality")) if x), "success")),
    "file.upgraded": ("Upgraded to a better copy", True, lambda d: (
        f"Upgraded {d.get('name', '')}", d.get("path") or "", "success")),
    "download.failed": ("A download failed", True, lambda d: (
        f"Download failed: {d.get('name', '')}",
        (d.get("error") or "") + "\nFetchbox looks for another release.", "error")),
    "import.failed": ("A finished download couldn't be imported", True, lambda d: (
        f"Couldn't import {d.get('name', '')}", d.get("error") or "", "warning")),
    "storage.offline": ("A library folder is offline or unmounted", True, lambda d: (
        f"Library folder offline: {d.get('name', '')}",
        f"{d.get('path', '')}\n{d.get('message', '')}".strip(), "error")),
    "storage.low_space": ("A library folder is low on space", True, lambda d: (
        f"Low on space: {d.get('name', '')}", d.get("message") or "", "warning")),
    "release.grabbed": ("A release was grabbed (download starting)", False, lambda d: (
        f"Grabbed {d.get('name', '')}",
        " · ".join(x for x in (d.get("release_title"), d.get("quality"), d.get("indexer")) if x),
        "info")),
    "source.failed": ("An indexer is failing", False, lambda d: (
        f"Indexer failing: {d.get('name', '')}", d.get("error") or "", "warning")),
    "media_server.refresh_failed": ("A media server couldn't rescan", False, lambda d: (
        f"{d.get('name', '')} couldn't rescan", d.get("error") or "", "warning")),
    "system.health_changed": ("Fetchbox's health changed", False, lambda d: (
        f"Fetchbox health: {d.get('status', '')}", "Details under Settings > This computer",
        _health_level(d.get("status", "")))),
}  # fmt: skip
DEFAULT_EVENTS = [k for k, (_, on, _) in EVENTS.items() if on]


def message(ev: Event) -> Notification | None:
    spec = EVENTS.get(ev.type)
    if spec is None:
        return None
    title, body, level = spec[2](ev.data)
    return Notification(
        event=ev.type, title=title.strip(), body=body.strip(), level=level,
        occurred_at=ev.occurred_at, data=ev.data,
    )  # fmt: skip


def _invalid(exc: ValidationError, prefix: str) -> ProblemError:
    return ProblemError(
        422,
        "Validation Failed",
        slug="validation",
        errors=[
            FieldError(
                field=".".join(str(p) for p in (prefix, *e["loc"])),
                message=e["msg"].removeprefix("Value error, "),
            )
            for e in exc.errors()
        ],
    )


class NotificationsService:
    def __init__(
        self, db: Database, bus: EventBus, box: SecretBox, providers: ProviderRegistry
    ) -> None:
        self.db = db
        self.bus = bus
        self.box = box
        self.providers = providers
        self.provider_kwargs: dict[str, Any] = {}  # tests: a fake transport
        self.public_only: Callable[[], bool] = lambda: False  # Settings > Network
        self._tasks: set[asyncio.Task[None]] = set()

        @bus.on(*EVENTS)
        async def on_event(ev: Event) -> None:
            n = message(ev)
            if n is None:
                return
            for t in await self.all():
                if t.enabled and ev.type in (t.events or []):
                    task = asyncio.create_task(self._send(t, n), name=f"notify:{t.name}")
                    self._tasks.add(task)
                    task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("notify.task_failed", error=repr(task.exception()))

    async def stop(self) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=10)

    async def _send(self, t: NotificationTarget, n: Notification) -> None:
        error: str | None = None
        try:
            await self._provider(t).send(n)
        except NotifierError as exc:
            error = exc.message
        except Exception as exc:  # never let a notifier break the event bus
            log.exception("notify.failed", target=t.name)
            error = f"{type(exc).__name__}: {exc}"
        if error:
            log.warning("notify.failed", target=t.name, event_type=n.event, error=error)
        await self._record(t.id, error)

    async def _record(self, target_id: str, error: str | None) -> None:
        """Keep a send's outcome on the target. It's bookkeeping, not an edit: written
        without bumping the version, so it never makes a save of the target fail (412)."""
        values: dict[str, Any] = {"last_error": error}
        if error is None:
            values["last_sent_at"] = utcnow()
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(NotificationTarget)
                .where(NotificationTarget.id == target_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            await s.commit()

    def _provider(self, row: NotificationTarget) -> NotifierProvider:
        cls = self.providers.get("notifier", row.kind)
        settings = open_settings(
            self.box, cls.Settings, row.settings, context=f"notification_target:{row.id}"
        )
        kwargs: dict[str, Any] = {"public_only": self.public_only(), **self.provider_kwargs}
        p = cls(settings, **kwargs)
        assert isinstance(p, NotifierProvider)
        return p

    # ---- targets ------------------------------------------------------------------------------

    def kinds(self) -> list[str]:
        return [c.kind for c in self.providers.of("notifier")]

    def view(self, row: NotificationTarget) -> dict[str, Any]:
        return mask_settings(self.providers.get("notifier", row.kind).Settings, row.settings)

    @staticmethod
    def _events(events: list[str] | None) -> list[str]:
        if events is None:
            return list(DEFAULT_EVENTS)
        unknown = sorted(set(events) - set(EVENTS))
        if unknown:
            raise ProblemError(
                422, "Validation Failed", slug="validation",
                errors=[FieldError(field="body.events",
                                   message=f"unknown event(s) {', '.join(unknown)}")],
            )  # fmt: skip
        return [e for e in EVENTS if e in events]  # catalogue order

    async def all(self) -> list[NotificationTarget]:
        async with self.db.sessionmaker() as s:
            q = select(NotificationTarget).order_by(NotificationTarget.name)
            return list((await s.execute(q)).scalars())

    async def get(self, target_id: str) -> NotificationTarget:
        async with self.db.sessionmaker() as s:
            row = await s.get(NotificationTarget, target_id)
        if row is None:
            raise NotFoundError("notification target", target_id)
        return row

    async def create(self, data: dict[str, Any], *, actor: Actor) -> NotificationTarget:
        try:
            cls = self.providers.get("notifier", data["kind"])
        except LookupError:
            raise ProblemError(
                422, "Validation Failed", slug="validation",
                errors=[FieldError(field="body.kind",
                                   message=f"unknown kind; known: {', '.join(self.kinds())}")],
            ) from None  # fmt: skip
        try:
            model = cls.Settings.model_validate(data.get("settings") or {})
        except ValidationError as exc:
            raise _invalid(exc, "body.settings") from None
        row = NotificationTarget(
            id=new_id(),
            kind=data["kind"],
            name=data["name"],
            enabled=data.get("enabled", True),
            events=self._events(data.get("events")),
        )
        row.settings = seal_settings(self.box, model, context=f"notification_target:{row.id}")
        async with self.db.sessionmaker() as s:
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                raise ConflictError(f"a notification target named {row.name!r} exists") from None
        await self.bus.emit(
            "notifications.target_added",
            subject=Subject(type="notification_target", id=row.id),
            actor=actor,
            name=row.name,
            kind=row.kind,
        )
        return row

    async def update(
        self, target_id: str, changes: dict[str, Any], *, if_match: str | None, actor: Actor
    ) -> NotificationTarget:
        async with self.db.sessionmaker() as s:
            row = await s.get(NotificationTarget, target_id)
            if row is None:
                raise NotFoundError("notification target", target_id)
            check_if_match(row, if_match)
            cls = self.providers.get("notifier", row.kind)
            if "settings" in changes:
                current = open_settings(
                    self.box, cls.Settings, row.settings, context=f"notification_target:{row.id}"
                ).model_dump(mode="python")
                current = {k: (v.get_secret_value() if hasattr(v, "get_secret_value") else v)
                           for k, v in current.items()}  # fmt: skip
                try:
                    model = cls.Settings.model_validate(
                        merge_update(cls.Settings, current, changes["settings"])
                    )
                except ValidationError as exc:
                    raise _invalid(exc, "body.settings") from None
                row.settings = seal_settings(
                    self.box, model, context=f"notification_target:{row.id}"
                )
            if "events" in changes:
                row.events = self._events(changes["events"])
            for key in ("name", "enabled"):
                if key in changes:
                    setattr(row, key, changes[key])
            try:
                await s.commit()
            except IntegrityError:
                raise ConflictError(f"a target named {changes.get('name')!r} exists") from None
        return row

    async def delete(self, target_id: str, *, if_match: str | None, actor: Actor) -> None:
        async with self.db.sessionmaker() as s:
            row = await s.get(NotificationTarget, target_id)
            if row is None:
                raise NotFoundError("notification target", target_id)
            check_if_match(row, if_match)
            await s.delete(row)
            await s.commit()

    async def test(self, target_id: str) -> TestResult:
        res = await self._provider(await self.get(target_id)).test()
        await self._record(target_id, None if res.ok else res.message)
        return res
