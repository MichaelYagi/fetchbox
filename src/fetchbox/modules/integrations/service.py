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
import os
import time
from collections import Counter
from pathlib import PurePath
from typing import Any, Literal

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
    place,
    same_path,
)

log = structlog.get_logger(__name__)

GATHER_SECONDS = 5.0
SAMPLE = 300  # files asked of each server library to line up with Fetchbox's
LEARN_SECONDS = 6 * 3600  # how long what the files said is trusted before asking again
LOCAL_FILES = 20000
PRODUCTS = {"plex": "Plex", "jellyfin": "Jellyfin", "emby": "Emby"}
EVENTS = (
    "import.completed",
    "file.deleted",  # (from disk: its folder, so Plex drops it)
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


def _join(like: str, parts: list[str]) -> str:
    """Folder names joined in the style of the path ``like`` (/a/b, D:\\a, \\\\nas\\a)."""
    sep = "\\" if "\\" in like and "/" not in like else "/"
    lead = "\\\\" if like.startswith("\\\\") else "/" if like.startswith("/") else ""
    return lead + sep.join(parts)


def learn_mappings(local: list[tuple[str, str]], remote: list[str]) -> list[PathMapping]:
    """Where the server sees each library folder, from files both know: Fetchbox's
    ``(library folder, path inside it)`` and the server's full paths. A server file
    ending in the same path inside the folder gives the folder's place on the server
    (``/Volumes/Public/Movies`` + ``Dune (2021)/Dune.mkv`` vs ``/volume1/Public/Movies/
    Dune (2021)/Dune.mkv``: ``/volume1/Public/Movies``). The most-agreed place wins."""
    by_name: dict[str, list[tuple[str, list[str]]]] = {}
    for root, rel in local:
        parts = [x.casefold() for x in _parts(rel)]
        if parts:
            by_name.setdefault(parts[-1], []).append((root, parts))
    votes: Counter[tuple[str, str]] = Counter()
    for path in remote:
        theirs = _parts(path)
        if not theirs:
            continue
        for root, parts in by_name.get(theirs[-1].casefold(), []):
            n = len(parts)
            if len(theirs) > n and [x.casefold() for x in theirs[-n:]] == parts:
                votes[(root, _join(path, theirs[:-n]))] += 1
    best: dict[str, tuple[int, str]] = {}
    for (root, there), n in votes.most_common():
        best.setdefault(root, (n, there))
    return [
        PathMapping(local=root, remote=there)
        for root, (_, there) in sorted(best.items())
        if not same_path(root, there)
    ]


def guess_mappings(path: str, libraries: list[Library]) -> list[PathMapping]:
    """When no shared file says, a guess from the names: the server library folder
    sharing the most trailing folder names with ``path`` (or one of its parents), e.g.
    /Volumes/Public/Media/Movies and /volume1/Public/Media/Movies. Several equally good
    places are all returned (the caller asks the server which one it has)."""
    mine = _parts(path)
    best: dict[str, PathMapping] = {}  # the path as each place would have it -> mapping
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
                m = PathMapping(local=_join(path, head), remote=root)
                if k > score:
                    score, best = k, {}
                best[to_remote(path, [m])] = m
    return list(best.values())


Where = Literal["", "mapping", "files", "names"]


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
        self._learned: dict[str, tuple[float, list[PathMapping]]] = {}  # server id ->

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
            places = await self._places(row, provider, libs, mappings, folders)
            remote = sorted(set(places.values()))
            wanted = []
            product = PRODUCTS.get(row.kind, row.name)
            for local, there in sorted(places.items()):
                # a folder that's here but not there: the scan would quietly do nothing.
                # (One gone from here too, e.g. renamed away, is scanned so it's dropped.)
                here: bool = await asyncio.to_thread(os.path.isdir, local)
                if here and covering(libs, there) and await provider.exists(there) is False:
                    problems.append(f"{product} can't see {there} (here it's {local})")
                elif there not in wanted:
                    wanted.append(there)
            if wanted:
                problems += await provider.refresh(sorted(wanted))
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
        self._learned.pop(server_id, None)
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
        self._learned.pop(server_id, None)
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
        (``folders``: [(name, local path)]), as the server sees them, and whether the
        server can see that folder."""
        row = await self.get(server_id)
        mappings = [PathMapping.model_validate(m) for m in row.path_mappings or []]
        provider = self._provider(row)
        coverage = []
        try:
            res: TestResult = await provider.test()
            libs = (
                [Library.model_validate(x) for x in res.details.get("libraries", [])]
                if res.ok
                else []
            )
            if res.ok:
                learned = await self._known(row, provider, libs, fresh=True)
                for name, local in folders:
                    remote, how = await self._where(local, mappings, learned, libs, provider)
                    lib = covering(libs, remote)
                    coverage.append(
                        {"folder": name, "local": local, "remote": remote,
                         "library": lib.name if lib else None, "how": how,
                         "found": how in ("files", "names"),
                         "visible": await provider.exists(remote) if lib else None}
                    )  # fmt: skip
        finally:
            await provider.aclose()
        return {
            "ok": res.ok,
            "message": res.message,
            "libraries": [lib.model_dump() for lib in libs],
            "coverage": coverage,
        }

    # ---- where the server sees Fetchbox's folders ---------------------------------------------

    async def _places(
        self,
        row: MediaServer,
        provider: MediaServerProvider,
        libs: list[Library],
        mappings: list[PathMapping],
        folders: list[str],
    ) -> dict[str, str]:
        """Each folder as the server sees it; what the files said is asked again (once)
        when it leaves a folder outside every library, e.g. a library added since."""
        cached = row.id in self._learned
        learned = await self._known(row, provider, libs)
        out = {f: (await self._where(f, mappings, learned, libs, provider))[0] for f in folders}
        if cached and any(covering(libs, x) is None for x in out.values()):
            learned = await self._known(row, provider, libs, fresh=True)
            out = {f: (await self._where(f, mappings, learned, libs, provider))[0] for f in folders}
        return out

    async def _where(
        self,
        path: str,
        mappings: list[PathMapping],
        learned: list[PathMapping],
        libs: list[Library],
        provider: MediaServerProvider,
    ) -> tuple[str, Where]:
        """``path`` as the server sees it, and how that's known: the user's mapping, the
        files both know, the folder names, or "" (as it is: same machine, or not found)."""
        mine = to_remote(path, mappings)
        if found := place(libs, mine):
            return found[1], "mapping" if mine != path else ""
        there = to_remote(path, learned)
        if there != path and (found := place(libs, there)):
            return found[1], "files"
        guesses = guess_mappings(path, libs)
        if len(guesses) > 1:  # several equally good: the one the server has
            guesses = [m for m in guesses if await provider.exists(to_remote(path, [m]))]
        if len(guesses) == 1:
            there = to_remote(path, guesses)
            found = place(libs, there)
            return (found[1] if found else there), "names"
        return mine, ""

    async def _known(
        self,
        row: MediaServer,
        provider: MediaServerProvider,
        libs: list[Library],
        *,
        fresh: bool = False,
    ) -> list[PathMapping]:
        """Where the server sees each library folder, from files both know (cached)."""
        hit = self._learned.get(row.id)
        if hit and not fresh and time.monotonic() - hit[0] < LEARN_SECONDS:
            return hit[1]
        theirs: list[str] = []
        for lib in libs:
            try:
                theirs += await provider.files(lib, SAMPLE)
            except MediaServerError:
                continue
        learned = learn_mappings(await self._local_files(), theirs) if theirs else []
        self._learned[row.id] = (time.monotonic(), learned)
        return learned

    async def _local_files(self) -> list[tuple[str, str]]:
        from fetchbox.modules.library.models import MediaFile
        from fetchbox.modules.storage.models import Destination

        async with self.db.sessionmaker() as s:
            rows = await s.execute(
                select(Destination.path, MediaFile.relative_path)
                .join(Destination, Destination.id == MediaFile.destination_id)
                .order_by(MediaFile.id.desc())
                .limit(LOCAL_FILES)
            )
            return [(str(a), str(b)) for a, b in rows.all()]


def folder_of(path: str) -> str:
    return str(PurePath(path).parent)
