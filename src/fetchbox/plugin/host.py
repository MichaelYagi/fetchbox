"""The plugin host: loads a plugin into a running Fetchbox and runs what it contributes.

- An add-on's ``@on`` methods run as background jobs for each matching public event (and,
  with ``applies_to``, only for titles of those kinds), after the event's own work: a slow
  or failing add-on never delays an import. A failure is retried like any job.
- ``@every`` methods are schedules, shown in the job list with their last run.
- ``@action`` methods are jobs run on request (``run_action``); the buttons come with the
  plugin UI.
- A media kind is added to Fetchbox's list of kinds.

Unloading undoes all of it. Installing plugins (finding them, approving them, turning them
off in Settings) builds on this.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from pydantic import ValidationError

from fetchbox.core import kinds
from fetchbox.core.db import Database
from fetchbox.core.events import Event, EventBus
from fetchbox.core.jobs import JobContext, PermanentJobError
from fetchbox.core.schedules import ScheduleDef, Scheduler
from fetchbox.plugin.context import PluginContext, check_id
from fetchbox.plugin.events import INTERNAL_TYPES, to_public
from fetchbox.plugin.services import Services
from fetchbox_plugin import Addon, MediaKind, Plugin, PublicEvent, Settings
from fetchbox_plugin.manifest import SUPPORTED_APIS

log = structlog.get_logger(__name__)

JOB_TIME_LIMIT = 600.0  # seconds: a plugin's job that runs longer is stopped (and retried)
MAX_JOB_TYPE = 100  # the job table's type column
MAX_SCHEDULE_NAME = 64


class PluginLoadError(Exception):
    """A plugin that can't be loaded; the message says why."""


@dataclass
class _OnHandler:
    events: frozenset[str]
    applies_to: frozenset[str] | None
    job_type: str


@dataclass
class Loaded:
    plugin: type[Plugin]
    ctx: PluginContext
    addons: dict[str, Addon] = field(default_factory=dict)
    kinds: dict[str, MediaKind] = field(default_factory=dict)
    on: list[_OnHandler] = field(default_factory=list)
    job_types: list[str] = field(default_factory=list)
    schedules: list[str] = field(default_factory=list)
    actions: dict[tuple[str, str], str] = field(default_factory=dict)  # (addon, method) -> job
    bus_handler: Any = None


class PluginHost:
    def __init__(
        self,
        *,
        db: Database,
        bus: EventBus,
        scheduler: Scheduler,
        services: Services,
        data_dir: Path,
        settings_for: Callable[[str, str], dict[str, Any]] = lambda plugin, part: {},
    ) -> None:
        self.db = db
        self.bus = bus
        self.scheduler = scheduler
        self.services = services
        self.data_dir = data_dir
        self.settings_for = settings_for  # (plugin id, contribution id) -> stored settings
        self.loaded: dict[str, Loaded] = {}

    # ---- loading ------------------------------------------------------------------------

    async def load(self, plugin: type[Plugin]) -> Loaded:
        pid = self._check(plugin)
        ctx = PluginContext(pid, db=self.db, data_dir=self.data_dir, services=self.services)
        got = Loaded(plugin, ctx)
        try:
            for part in plugin.contributes:
                if isinstance(part, type) and issubclass(part, MediaKind):
                    self._add_kind(got, part)
                elif isinstance(part, type) and issubclass(part, Addon):
                    self._add_addon(got, part)
                # providers keep loading through the fetchbox.providers entry point
            if got.on:
                got.bus_handler = self._on_events(got)
                self.bus.on(*INTERNAL_TYPES)(got.bus_handler)
            if got.schedules:
                await self.scheduler.sync()
        except Exception:
            self._undo(got)
            await ctx.aclose()
            raise
        self.loaded[pid] = got
        log.info("plugin.loaded", plugin=pid, contributes=[c.__name__ for c in plugin.contributes])
        return got

    async def unload(self, plugin_id: str) -> None:
        got = self.loaded.pop(plugin_id, None)
        if got is None:
            return
        self._undo(got)
        await got.ctx.aclose()
        log.info("plugin.unloaded", plugin=plugin_id)

    async def run_action(self, plugin_id: str, addon: str, method: str, item: Any) -> str | None:
        """Run an add-on's @action for an item (a title, group or item id); the job's id."""
        got = self.loaded.get(plugin_id)
        job_type = got.actions.get((addon, method)) if got else None
        if job_type is None:
            raise LookupError(f"{plugin_id}: no action {addon}.{method}")
        job = await self.services.jobs.enqueue(job_type, {"item": item})
        return job.id if job else None

    def _check(self, plugin: type[Plugin]) -> str:
        if not (isinstance(plugin, type) and issubclass(plugin, Plugin)):
            raise PluginLoadError(f"{plugin!r} isn't a Plugin class")
        pid = getattr(plugin, "id", "")
        try:
            check_id(pid)
        except ValueError as exc:
            raise PluginLoadError(str(exc)) from None
        if plugin.fetchbox_api not in SUPPORTED_APIS:
            raise PluginLoadError(
                f"{pid} is written for plugin API {plugin.fetchbox_api}; this Fetchbox "
                f"supports API {', '.join(map(str, sorted(SUPPORTED_APIS)))}"
            )
        if pid in self.loaded:
            raise PluginLoadError(f"{pid} is already loaded")
        return pid

    def _add_kind(self, got: Loaded, part: type[MediaKind]) -> None:
        pid = got.ctx.plugin_id
        try:
            kinds.register(
                kinds.MediaKind(
                    key=part.id,
                    label=part.name,
                    plural=part.name,
                    levels=tuple(part.levels),
                    file_exts=frozenset(e.lstrip(".") for e in part.file_types),
                    category=f"fetchbox-{part.id}",
                    profile_media=part.id,
                    plugin=pid,
                )
            )
        except kinds.KindError as exc:
            raise PluginLoadError(f"{pid}: {exc}") from None
        kind = part()
        kind.settings = self._settings(pid, part.id, part.settings_model)
        got.kinds[part.id] = kind

    def _add_addon(self, got: Loaded, part: type[Addon]) -> None:
        pid = got.ctx.plugin_id
        addon = part()
        addon.settings = self._settings(pid, part.id, part.settings_model)
        got.addons[part.id] = addon
        found = part.handlers()
        for name, spec in found["on"]:
            job = self._job(got, f"plugin.{pid}.{part.id}.{name}", addon, name, "event")
            applies = frozenset(part.applies_to) if part.applies_to else None
            got.on.append(_OnHandler(frozenset(spec.events), applies, job))
        for name, spec in found["every"]:
            job = self._job(got, f"plugin.{pid}.{part.id}.{name}", addon, name, "schedule")
            sched = job if len(job) <= MAX_SCHEDULE_NAME else _short(pid, job)
            self.scheduler.define(
                ScheduleDef(
                    name=sched,
                    job_type=job,
                    every=spec.interval,
                    description=f"{got.plugin.name}: {name.replace('_', ' ')}",
                )
            )
            got.schedules.append(sched)
        for name, _spec in found["action"]:
            job = self._job(got, f"plugin.{pid}.{part.id}.{name}", addon, name, "action")
            got.actions[(part.id, name)] = job

    def _settings(self, pid: str, part: str, model: type[Settings] | None) -> Settings | None:
        if model is None:
            return None
        try:
            return model.model_validate(self.settings_for(pid, part))
        except ValidationError as exc:
            wrong = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or part}: {e['msg']}" for e in exc.errors()
            )
            raise PluginLoadError(
                f"{pid}: its settings aren't complete or valid ({wrong})"
            ) from None

    def _job(self, got: Loaded, job_type: str, addon: Addon, method: str, how: str) -> str:
        if len(job_type) > MAX_JOB_TYPE:
            raise PluginLoadError(f"{job_type}: over {MAX_JOB_TYPE} characters; use shorter ids")
        fn = getattr(addon, method)
        ctx = got.ctx

        async def run(job: JobContext) -> dict[str, Any]:
            async with asyncio.timeout(JOB_TIME_LIMIT):
                if how == "event":
                    try:
                        event = PublicEvent.model_validate(job.payload["event"])
                    except (KeyError, ValueError) as exc:
                        raise PermanentJobError(f"not an event: {exc}") from None
                    out = await fn(ctx, event)
                elif how == "action":
                    out = await fn(ctx, job.payload.get("item"))
                else:
                    out = await fn(ctx)
            return out if isinstance(out, dict) else {}

        self.services.jobs.register(job_type, run, queue="io")
        got.job_types.append(job_type)
        return job_type

    def _on_events(self, got: Loaded) -> Any:
        async def handle(ev: Event) -> None:
            pub = to_public(ev)
            if pub is not None:
                await self._dispatch_to(got, pub)

        return handle

    async def dispatch(self, event: PublicEvent) -> list[str]:
        """Run every loaded plugin's handlers for a public event, as a real one would; the
        ids of the jobs queued (the test kit's emit)."""
        ids: list[str] = []
        for got in list(self.loaded.values()):
            ids += await self._dispatch_to(got, event)
        return ids

    async def _dispatch_to(self, got: Loaded, pub: PublicEvent) -> list[str]:
        ids = []
        for h in got.on:
            if pub.type not in h.events:
                continue
            if h.applies_to is not None and pub.kind not in h.applies_to:
                continue
            payload = {"event": pub.model_dump(mode="json")}
            key = f"{h.job_type}:{pub.id}"  # one run per event
            job = await self.services.jobs.enqueue(h.job_type, payload, dedupe_key=key)
            if job is not None:
                ids.append(job.id)
        return ids

    def _undo(self, got: Loaded) -> None:
        if got.bus_handler is not None:
            self.bus.off(got.bus_handler)
        for name in got.schedules:
            self.scheduler.undefine(name)
        for t in got.job_types:
            self.services.jobs.unregister(t)
        kinds.unregister(got.ctx.plugin_id)


def _short(plugin_id: str, job_type: str) -> str:
    digest = hashlib.sha1(job_type.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"plugin.{plugin_id[: MAX_SCHEDULE_NAME - 20]}.{digest}"
