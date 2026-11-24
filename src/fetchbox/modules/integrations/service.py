"""Media-server connections: Plex / Jellyfin / Emby rescan what Fetchbox changes.

Imports, upgrades, renames and existing-library imports announce the folders they
touched; those are gathered for a few seconds (a season pack is one refresh, not ten),
mapped to the server's view of the paths, and each enabled server is asked to rescan
just those folders. Failures are recorded on the connection and logged, never raised:
a media server being down mustn't affect downloads.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import PurePath
from typing import Any

import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
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
from fetchbox.modules.integrations.models import MediaServer
from fetchbox.providers.media_servers import (
    Library,
    MediaServerError,
    MediaServerProvider,
    covering,
)

log = structlog.get_logger(__name__)

GATHER_SECONDS = 5.0
EVENTS = (
    "import.completed",
    "library.renamed",
    "library.imported_existing",
    "library.folder_changed",
)


class PathMapping(BaseModel):
    local: str = Field(min_length=1, description="Path as Fetchbox sees it")
    remote: str = Field(min_length=1, description="The same path as the media server sees it")


def to_remote(path: str, mappings: list[PathMapping]) -> str:
    """Fetchbox's path as the server sees it (longest matching prefix wins)."""
    norm = path.replace("\\", "/")
    for m in sorted(mappings, key=lambda m: len(m.local), reverse=True):
        local = m.local.replace("\\", "/").rstrip("/")
        if norm == local or norm.startswith(local + "/"):
            remote = m.remote.rstrip("/\\")
            sep = "\\" if "\\" in remote and "/" not in remote else "/"
            return remote + norm[len(local) :].replace("/", sep)
    return path


def _parts(path: str) -> list[str]:
    return [x for x in path.replace("\\", "/").split("/") if x]


def guess_mapping(path: str, libraries: list[Library]) -> PathMapping | None:
    """The same folder as a server on another machine sees it, worked out from its
    libraries: /Volumes/Public/Media/Movies on a Mac is /volume1/Public/Media/Movies on
    the NAS running Plex. The library folder sharing the most trailing folder names with
    ``path`` (or one of its parents) wins; a tie between different places is no guess."""
    mine = _parts(path)
    best: dict[str, PathMapping] = {}  # translated path -> mapping
    score = 0
    for lib in libraries:
        for root in lib.paths:
            theirs = _parts(root)
            for cut in range(len(mine), 0, -1):  # path itself, then each parent
                head = mine[:cut]
                k = 0
                while k < min(len(head), len(theirs)) and (
                    head[-1 - k].casefold() == theirs[-1 - k].casefold()
                ):
                    k += 1
                if k == 0 or k < score:
                    continue
                local = ("/" if path.replace("\\", "/").startswith("/") else "") + "/".join(head)
                m = PathMapping(local=local, remote=root)
                if k > score:
                    score, best = k, {}
                best[to_remote(path, [m])] = m
    return next(iter(best.values())) if len(best) == 1 else None


def server_path(
    path: str, mappings: list[PathMapping], libraries: list[Library]
) -> tuple[str, bool]:
    """``path`` as the server sees it: by the user's path mappings, else, when no library
    holds it that way, worked out from the server's libraries. (path, worked out?)"""
    remote = to_remote(path, mappings)
    if covering(libraries, remote) is None and (m := guess_mapping(path, libraries)):
        return to_remote(path, [m]), True
    return remote, False


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


class IntegrationsService:
    def __init__(
        self, db: Database, bus: EventBus, box: SecretBox, providers: ProviderRegistry
    ) -> None:
        self.db = db
        self.bus = bus
        self.box = box
        self.providers = providers
        self.gather_seconds = GATHER_SECONDS
        self.provider_kwargs: dict[str, Any] = {}  # tests: a fake transport
        self._pending: set[str] = set()
        self._flush: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

        @bus.on(*EVENTS)
        async def changed(ev: Event) -> None:
            folders = ev.data.get("folders") or []
            if not folders:
                return
            self._pending.update(str(f) for f in folders)
            if self._flush is None or self._flush.done():
                self._flush = asyncio.create_task(self._later(), name="media-server-refresh")

    async def stop(self) -> None:
        if self._flush is not None and not self._flush.done():
            self._flush.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush

    # ---- refresh ------------------------------------------------------------------------------

    async def _later(self) -> None:
        await asyncio.sleep(self.gather_seconds)
        await self.refresh_pending()

    async def refresh_pending(self) -> None:
        async with self._lock:
            folders, self._pending = sorted(self._pending), set()
            if not folders:
                return
            for row in await self.all():
                if row.enabled:
                    await self._refresh_one(row, folders)

    async def _refresh_one(self, row: MediaServer, folders: list[str]) -> None:
        mappings = [PathMapping.model_validate(m) for m in row.path_mappings or []]
        remote = sorted({to_remote(f, mappings) for f in folders})
        error: str | None = None
        problems: list[str] = []
        provider = self._provider(row)
        try:
            # a server on another machine (Plex on a NAS) sees these folders elsewhere
            libs = await provider.libraries()
            remote = sorted({server_path(f, mappings, libs)[0] for f in folders})
            problems = await provider.refresh(remote)
        except MediaServerError as exc:
            error = exc.message
        except Exception as exc:  # a broken server must not break imports
            log.exception("media_server.refresh_failed", server=row.name)
            error = f"{type(exc).__name__}: {exc}"
        finally:
            await provider.aclose()
        if problems and not error:
            error = "; ".join(problems) + (
                " (add that folder to a library in the server, or a path mapping in "
                "Fetchbox if the server sees it under another name)"
            )
        async with self.db.sessionmaker() as s:
            r = await s.get(MediaServer, row.id)
            if r is not None:
                r.last_refresh_at, r.last_error = utcnow(), error
                await s.commit()
        await self.bus.emit(
            "media_server.refreshed" if error is None else "media_server.refresh_failed",
            subject=Subject(type="media_server", id=row.id),
            name=row.name,
            folders=remote,
            error=error,
        )

    # ---- connections --------------------------------------------------------------------------

    def _provider(self, row: MediaServer) -> MediaServerProvider:
        cls = self.providers.get("media_server", row.kind)
        settings = open_settings(
            self.box, cls.Settings, row.settings, context=f"media_server:{row.id}"
        )
        p = cls(settings, **self.provider_kwargs)
        assert isinstance(p, MediaServerProvider)
        return p

    def kinds(self) -> list[str]:
        return [c.kind for c in self.providers.of("media_server")]

    def view(self, row: MediaServer) -> dict[str, Any]:
        cls = self.providers.get("media_server", row.kind)
        return mask_settings(cls.Settings, row.settings)

    async def all(self) -> list[MediaServer]:
        async with self.db.sessionmaker() as s:
            return list((await s.execute(select(MediaServer).order_by(MediaServer.name))).scalars())

    async def get(self, server_id: str) -> MediaServer:
        async with self.db.sessionmaker() as s:
            row = await s.get(MediaServer, server_id)
        if row is None:
            raise NotFoundError("media server", server_id)
        return row

    async def create(self, data: dict[str, Any], *, actor: Actor) -> MediaServer:
        try:
            cls = self.providers.get("media_server", data["kind"])
        except LookupError:
            raise ProblemError(
                422,
                "Validation Failed",
                slug="validation",
                errors=[FieldError(field="body.kind",
                                   message=f"unknown kind; known: {', '.join(self.kinds())}")],
            ) from None  # fmt: skip
        try:
            model = cls.Settings.model_validate(data.get("settings") or {})
            mappings = [PathMapping.model_validate(m) for m in data.get("path_mappings") or []]
        except ValidationError as exc:
            raise _invalid(exc, "body") from None
        row = MediaServer(
            id=new_id(),
            kind=data["kind"],
            name=data["name"],
            enabled=data.get("enabled", True),
            path_mappings=[m.model_dump() for m in mappings],
        )
        row.settings = seal_settings(self.box, model, context=f"media_server:{row.id}")
        async with self.db.sessionmaker() as s:
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                raise ConflictError(f"a media server named {row.name!r} exists") from None
        await self.bus.emit(
            "media_server.added",
            subject=Subject(type="media_server", id=row.id),
            actor=actor,
            name=row.name,
            kind=row.kind,
        )
        return row

    async def update(
        self, server_id: str, changes: dict[str, Any], *, if_match: str | None, actor: Actor
    ) -> MediaServer:
        async with self.db.sessionmaker() as s:
            row = await s.get(MediaServer, server_id)
            if row is None:
                raise NotFoundError("media server", server_id)
            check_if_match(row, if_match)
            cls = self.providers.get("media_server", row.kind)
            try:
                if "settings" in changes:
                    current = open_settings(
                        self.box, cls.Settings, row.settings, context=f"media_server:{row.id}"
                    ).model_dump(mode="python")
                    current = {k: (v.get_secret_value() if hasattr(v, "get_secret_value") else v)
                               for k, v in current.items()}  # fmt: skip
                    model = cls.Settings.model_validate(
                        merge_update(cls.Settings, current, changes["settings"])
                    )
                    row.settings = seal_settings(self.box, model, context=f"media_server:{row.id}")
                if "path_mappings" in changes:
                    row.path_mappings = [
                        PathMapping.model_validate(m).model_dump()
                        for m in changes["path_mappings"] or []
                    ]
            except ValidationError as exc:
                raise _invalid(exc, "body") from None
            for key in ("name", "enabled"):
                if key in changes:
                    setattr(row, key, changes[key])
            try:
                await s.commit()
            except IntegrityError:
                raise ConflictError(
                    f"a media server named {changes.get('name')!r} exists"
                ) from None
        await self.bus.emit(
            "media_server.updated",
            subject=Subject(type="media_server", id=server_id),
            actor=actor,
            name=row.name,
        )
        return row

    async def delete(self, server_id: str, *, if_match: str | None, actor: Actor) -> None:
        async with self.db.sessionmaker() as s:
            row = await s.get(MediaServer, server_id)
            if row is None:
                raise NotFoundError("media server", server_id)
            check_if_match(row, if_match)
            await s.delete(row)
            await s.commit()
        await self.bus.emit(
            "media_server.removed",
            subject=Subject(type="media_server", id=server_id),
            actor=actor,
            name=row.name,
        )

    async def test(self, server_id: str, folders: list[tuple[str, str]]) -> dict[str, Any]:
        """Connection test, plus which library holds each of Fetchbox's library folders
        (``folders``: [(name, local path)]), as the server sees them."""
        row = await self.get(server_id)
        provider = self._provider(row)
        try:
            res: TestResult = await provider.test()
            libs = (
                [Library.model_validate(x) for x in res.details.get("libraries", [])]
                if res.ok
                else []
            )
        finally:
            await provider.aclose()
        mappings = [PathMapping.model_validate(m) for m in row.path_mappings or []]
        coverage = []
        for name, local in folders:
            remote, found = server_path(local, mappings, libs)
            lib = covering(libs, remote)
            coverage.append(
                {"folder": name, "local": local, "remote": remote,
                 "library": lib.name if lib else None, "found": found}
            )  # fmt: skip
        return {
            "ok": res.ok,
            "message": res.message,
            "libraries": [lib.model_dump() for lib in libs],
            "coverage": coverage,
        }


def folder_of(path: str) -> str:
    return str(PurePath(path).parent)
