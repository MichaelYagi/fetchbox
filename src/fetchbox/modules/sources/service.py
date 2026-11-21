"""Configured indexers: CRUD, provider instances, failure backoff, health.

An indexer that fails (unreachable, rate-limited, key refused) is skipped by searches
until ``disabled_until``; the wait doubles with each consecutive failure, from 5 minutes
up to 24 hours, and resets on the next success. A rate limit with Retry-After waits
exactly that long. Testing an indexer clears its backoff when the test passes.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from fetchbox.core.concurrency import check_if_match
from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, FieldError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.ids import new_id
from fetchbox.core.providers import HealthStatus, ProviderRegistry, TestResult
from fetchbox.core.secrets import MASK, SecretBox, mask_settings, open_settings, seal_settings
from fetchbox.modules.sources.models import Indexer, IndexerDefinition
from fetchbox.providers.indexers import (
    IndexerError,
    IndexerProvider,
    ReleaseCandidate,
    SearchQuery,
)
from fetchbox.providers.indexers.definition import (
    Definition,
    DefinitionError,
    builtin_definitions,
    catalog,
    parse_definition,
)

log = structlog.get_logger(__name__)

SYSTEM = Actor(type="system", id="setup", name="first run")
BACKOFF_START = timedelta(minutes=5)
BACKOFF_MAX = timedelta(hours=24)


def backoff(failures: int, retry_after: float | None = None) -> timedelta:
    if retry_after is not None and retry_after > 0:
        return min(timedelta(seconds=retry_after), BACKOFF_MAX)
    doubled: timedelta = BACKOFF_START * (2 ** min(max(failures - 1, 0), 16))
    return min(doubled, BACKOFF_MAX)


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


class IndexerSource:
    """One indexer as the release search sees it (``releases.search.ReleaseSource``)."""

    def __init__(self, svc: SourcesService, row: Indexer, provider: IndexerProvider) -> None:
        self.svc = svc
        self.id = row.id
        self.name = row.name
        self.priority = row.priority
        self.provider = provider

    async def search(self, query: SearchQuery) -> list[ReleaseCandidate]:
        try:
            found = await self.provider.search(query)
        except IndexerError as exc:
            await self.svc.record_failure(self.id, exc)
            raise
        await self.svc.record_success(self.id)
        return found


class SourcesService:
    def __init__(
        self, db: Database, bus: EventBus, box: SecretBox, providers: ProviderRegistry
    ) -> None:
        self.db = db
        self.bus = bus
        self.box = box
        self.providers = providers
        self._instances: dict[str, tuple[int, IndexerProvider]] = {}
        self.provider_kwargs: dict[str, Any] = {}  # tests inject a mock transport

    # ---- providers ----------------------------------------------------------------------------

    def kinds(self) -> list[type[IndexerProvider]]:
        return [c for c in self.providers.of("indexer") if issubclass(c, IndexerProvider)]

    def _cls(self, kind: str) -> type[IndexerProvider]:
        try:
            cls = self.providers.get("indexer", kind)
        except LookupError:
            known = ", ".join(c.kind for c in self.kinds()) or "none"
            raise ProblemError(
                422,
                "Validation Failed",
                slug="validation",
                errors=[FieldError(field="body.kind", message=f"unknown kind; known: {known}")],
            ) from None
        assert issubclass(cls, IndexerProvider)
        return cls

    def _validate(self, kind: str, data: dict[str, Any]) -> BaseModel:
        try:
            return self._cls(kind).Settings.model_validate(data)
        except ValidationError as exc:
            raise _invalid(exc, "body.settings") from None

    def _build(self, kind: str, settings: BaseModel) -> IndexerProvider:
        provider = self._cls(kind)(settings, **self.provider_kwargs)
        assert isinstance(provider, IndexerProvider)
        return provider

    async def provider(self, row: Indexer) -> IndexerProvider:
        cached = self._instances.get(row.id)
        if cached and cached[0] == row.version:
            return cached[1]
        if cached:
            await cached[1].aclose()
        settings = open_settings(
            self.box, self._cls(row.kind).Settings, row.settings, context=f"indexer:{row.id}"
        )
        provider = self._build(row.kind, settings)
        self._instances[row.id] = (row.version, provider)
        return provider

    async def _drop(self, indexer_id: str) -> None:
        if cached := self._instances.pop(indexer_id, None):
            await cached[1].aclose()

    async def aclose(self) -> None:
        for _, p in self._instances.values():
            await p.aclose()
        self._instances.clear()

    def masked(self, row: Indexer) -> dict[str, Any]:
        return mask_settings(self._cls(row.kind).Settings, row.settings)

    # ---- CRUD ---------------------------------------------------------------------------------

    async def all(self) -> list[Indexer]:
        async with self.db.sessionmaker() as s:
            q = select(Indexer).order_by(Indexer.priority, Indexer.name)
            return list((await s.execute(q)).scalars())

    async def get(self, indexer_id: str) -> Indexer:
        async with self.db.sessionmaker() as s:
            row = await s.get(Indexer, indexer_id)
        if row is None:
            raise NotFoundError("indexer", indexer_id)
        return row

    async def create(
        self,
        *,
        kind: str,
        name: str,
        settings: dict[str, Any],
        enabled: bool,
        priority: int,
        tags: list[str],
        actor: Actor,
    ) -> Indexer:
        model = self._validate(kind, settings)
        row = Indexer(id=new_id(), kind=kind, name=name, enabled=enabled, priority=priority,
                      tags=tags)  # fmt: skip
        row.settings = seal_settings(self.box, model, context=f"indexer:{row.id}")
        async with self.db.sessionmaker() as s:
            if kind == "definition":
                slug = str(getattr(model, "definition", ""))
                q = select(IndexerDefinition.id).where(IndexerDefinition.slug == slug)
                row.definition_id = await s.scalar(q)
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                raise ConflictError(f"an indexer named {name!r} already exists") from None
        await self._emit("source.added", row, actor)
        return row

    async def update(
        self, indexer_id: str, changes: dict[str, Any], *, if_match: str | None, actor: Actor
    ) -> Indexer:
        async with self.db.sessionmaker() as s:
            row = await s.get(Indexer, indexer_id)
            if row is None:
                raise NotFoundError("indexer", indexer_id)
            check_if_match(row, if_match)
            if "settings" in changes:
                cls = self._cls(row.kind).Settings
                current = open_settings(self.box, cls, row.settings, context=f"indexer:{row.id}")
                plain = current.model_dump(mode="python")
                incoming = dict(changes["settings"])
                for k, v in incoming.items():  # MASK sent back = keep that secret
                    if v == MASK:
                        incoming[k] = plain.get(k)
                model = self._validate(row.kind, {**plain, **incoming})
                row.settings = seal_settings(self.box, model, context=f"indexer:{row.id}")
            for f in ("name", "enabled", "priority", "tags"):
                if f in changes:
                    setattr(row, f, changes[f])
            if changes.get("enabled") is True or "settings" in changes:
                row.disabled_until, row.failure_count = None, 0  # a fresh start
            try:
                await s.commit()
            except IntegrityError:
                raise ConflictError(
                    f"an indexer named {changes.get('name')!r} already exists"
                ) from None
        await self._drop(indexer_id)
        await self._emit("source.updated", row, actor, fields=sorted(changes))
        return row

    async def delete(self, indexer_id: str, *, if_match: str | None, actor: Actor) -> None:
        async with self.db.sessionmaker() as s:
            row = await s.get(Indexer, indexer_id)
            if row is None:
                raise NotFoundError("indexer", indexer_id)
            check_if_match(row, if_match)
            await s.delete(row)
            await s.commit()
        await self._drop(indexer_id)
        await self._emit("source.removed", row, actor)

    async def reorder(self, ids: Sequence[str], *, actor: Actor) -> list[Indexer]:
        """Priorities 1..n in the given order; indexers not listed keep theirs."""
        async with self.db.sessionmaker() as s:
            rows = {r.id: r for r in (await s.execute(select(Indexer))).scalars()}
            missing = [i for i in ids if i not in rows]
            if missing:
                raise ProblemError(
                    422,
                    "Validation Failed",
                    slug="validation",
                    errors=[FieldError(field="body.ids", message=f"unknown indexer(s) {missing}")],
                )
            for n, i in enumerate(dict.fromkeys(ids), start=1):
                rows[i].priority = n
            await s.commit()
        await self.bus.emit("source.reordered", actor=actor, ids=list(ids))
        return await self.all()

    # ---- definitions --------------------------------------------------------------------------

    async def sync_definitions(self, *, seed: bool = True) -> None:
        """Install or update the built-in definitions and load every definition into the
        provider's catalog. On the very first install (no definitions yet) with ``seed``,
        add an enabled indexer for each built-in, so search works with no setup."""
        builtins = builtin_definitions()
        async with self.db.sessionmaker() as s:
            rows = {r.slug: r for r in (await s.execute(select(IndexerDefinition))).scalars()}
            first_install = not rows
            for d, text in builtins:
                row = rows.get(d.slug)
                if row is None:
                    row = IndexerDefinition(slug=d.slug, source="builtin", yaml=text)
                    s.add(row)
                    rows[d.slug] = row
                if row.source == "builtin":  # shipped with this version: always current
                    row.yaml, row.version, row.name = text, d.version, d.name
            await s.commit()
            for row in rows.values():
                try:
                    catalog.put(parse_definition(row.yaml), row.yaml)
                except DefinitionError as exc:  # a user definition this version can't read
                    log.warning("sources.definition_unreadable", slug=row.slug, error=str(exc))
        if first_install and seed:
            for d, _ in builtins:
                if d.settings and any(x.required for x in d.settings):
                    continue  # needs an account: the user adds it
                with contextlib.suppress(ConflictError):
                    await self.create(
                        kind="definition",
                        name=d.name,
                        settings={"definition": d.slug},
                        enabled=True,
                        priority=50,
                        tags=[],
                        actor=SYSTEM,
                    )

    async def definitions(self) -> list[tuple[IndexerDefinition, Definition]]:
        async with self.db.sessionmaker() as s:
            rows = (
                await s.execute(select(IndexerDefinition).order_by(IndexerDefinition.name))
            ).scalars()
            return [(r, d) for r in rows if (d := catalog.get(r.slug)) is not None]

    async def definition(self, slug: str) -> tuple[IndexerDefinition, Definition]:
        async with self.db.sessionmaker() as s:
            row = await s.scalar(select(IndexerDefinition).where(IndexerDefinition.slug == slug))
        d = catalog.get(slug)
        if row is None or d is None:
            raise NotFoundError("indexer definition", slug)
        return row, d

    async def add_definition(
        self, text: str, *, actor: Actor
    ) -> tuple[IndexerDefinition, Definition]:
        try:
            d = parse_definition(text)
        except DefinitionError as exc:
            raise ProblemError(
                422,
                "Validation Failed",
                "the definition is not valid",
                slug="validation",
                errors=[FieldError(field="body.yaml", message=m) for m in exc.errors],
            ) from None
        row = IndexerDefinition(slug=d.slug, name=d.name, version=d.version, source="user",
                                yaml=text)  # fmt: skip
        async with self.db.sessionmaker() as s:
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                raise ConflictError(f"a definition with slug {d.slug!r} exists") from None
        catalog.put(d, text)
        await self.bus.emit(
            "source.definition_added",
            subject=Subject(type="indexer_definition", id=row.id),
            actor=actor,
            name=d.name,
            slug=d.slug,
        )
        return row, d

    async def delete_definition(self, slug: str, *, actor: Actor) -> None:
        row, d = await self.definition(slug)
        if row.source == "builtin":
            raise ConflictError(f"{d.name} is built in; disable its indexer instead")
        async with self.db.sessionmaker() as s:
            if await s.scalar(select(Indexer.id).where(Indexer.definition_id == row.id)):
                raise ConflictError(f"indexers use {d.name}; remove them first")
            await s.delete(await s.get(IndexerDefinition, row.id))
            await s.commit()
        catalog.drop(slug)
        await self.bus.emit(
            "source.definition_removed",
            subject=Subject(type="indexer_definition", id=row.id),
            actor=actor,
            name=d.name,
            slug=slug,
        )

    # ---- runtime ------------------------------------------------------------------------------

    async def test(self, indexer_id: str) -> TestResult:
        row = await self.get(indexer_id)
        result = await (await self.provider(row)).test()
        if result.ok:
            await self.record_success(indexer_id)
        return result

    async def release_sources(self, *, now: datetime | None = None) -> list[IndexerSource]:
        """Enabled indexers that aren't backing off, best priority first."""
        now = now or utcnow()
        out = []
        for row in await self.all():
            if not row.enabled or (row.disabled_until and row.disabled_until > now):
                continue
            out.append(IndexerSource(self, row, await self.provider(row)))
        return out

    async def record_failure(self, indexer_id: str, exc: IndexerError) -> None:
        async with self.db.sessionmaker() as s:
            row = await s.get(Indexer, indexer_id)
            if row is None:
                return
            # Raw UPDATE: runtime state must not bump the version (the user's ETag).
            failures = row.failure_count + 1
            until = utcnow() + backoff(failures, exc.retry_after)
            await s.execute(
                update(Indexer)
                .where(Indexer.id == indexer_id)
                .values(failure_count=failures, disabled_until=until, last_error=exc.message)
                .execution_options(synchronize_session=False)
            )
            await s.commit()
        log.warning("indexer.failed", indexer=row.name, error=exc.message, until=until.isoformat())
        await self.bus.emit(
            "source.failed",
            subject=Subject(type="indexer", id=indexer_id),
            name=row.name,
            error=exc.message,
            disabled_until=until.isoformat(),
        )

    async def record_success(self, indexer_id: str) -> None:
        async with self.db.sessionmaker() as s:
            row = await s.get(Indexer, indexer_id)
            if row is None or (row.failure_count == 0 and row.disabled_until is None):
                return
            await s.execute(
                update(Indexer)
                .where(Indexer.id == indexer_id)
                .values(failure_count=0, disabled_until=None, last_error=None)
                .execution_options(synchronize_session=False)
            )
            await s.commit()
        await self.bus.emit(
            "source.recovered", subject=Subject(type="indexer", id=indexer_id), name=row.name
        )

    async def health(self) -> HealthStatus:
        rows = await self.all()
        enabled = [r for r in rows if r.enabled]
        if not enabled:
            return HealthStatus(
                status="warning",
                message=(
                    "No indexers: add Prowlarr (or a Torznab feed) in Settings > Indexers "
                    "to search for releases"
                ),
            )
        now = utcnow()
        backing_off = [r.name for r in enabled if r.disabled_until and r.disabled_until > now]
        if len(backing_off) == len(enabled):
            return HealthStatus(status="error", message="Every indexer is failing: " +
                                ", ".join(backing_off))  # fmt: skip
        if backing_off:
            return HealthStatus(status="warning", message="Failing, retried later: " +
                                ", ".join(backing_off))  # fmt: skip
        return HealthStatus(status="ok", message=f"{len(enabled)} indexer(s) enabled")

    async def _emit(self, type_: str, row: Indexer, actor: Actor, **data: Any) -> None:
        await self.bus.emit(
            type_,
            subject=Subject(type="indexer", id=row.id),
            actor=actor,
            name=row.name,
            kind=row.kind,
            **data,
        )
