"""Move from Radarr / Sonarr: their movies, shows and files, without touching them.

A scan (a job; read-only on both sides) reads what Radarr or Sonarr has: titles (by TMDB
id), what's monitored, quality profiles, root folders, which files exist, and the
indexers and download clients (listed only: they return secrets masked, so those are
re-added in Fetchbox, ideally as one Prowlarr). Each of their quality profiles is mapped
to a Fetchbox one by resolution, which the user can change.

Apply (a job) makes each root folder a Fetchbox library folder (through an optional
path translation, when Radarr sees the disk under another path), adds the titles
without searching, copies per-episode monitoring from Sonarr, and records existing
files in place (the same adoption as the existing-library import). Radarr and Sonarr are
never written to, so both can run side by side while you compare.

The Radarr / Sonarr API key is kept in memory for the job, never in the job's stored
payload.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from datetime import timedelta
from pathlib import PurePath
from typing import Any

import structlog
from sqlalchemy import update

from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, ProblemError
from fetchbox.core.events import Actor
from fetchbox.core.jobs import Job, JobContext, JobRunner
from fetchbox.modules.library.service import LibraryService
from fetchbox.modules.media.models import Title, WantedItem
from fetchbox.modules.media.service import MediaService
from fetchbox.modules.profiles.service import ProfilesService
from fetchbox.modules.storage.service import StorageService, _same_folder
from fetchbox.providers.arr import ArrClient, ArrError, Kind

log = structlog.get_logger(__name__)

SCAN_JOB = "migrate.scan"
APPLY_JOB = "migrate.apply"
FIRST_SEARCH_AFTER = timedelta(hours=1)
AVAILABILITY = {
    "announced": "announced",
    "inCinemas": "in_cinemas",
    "released": "released",
    "tba": "announced",
    "preDB": "released",
}


def guess_profile(name: str, ours: dict[str, str]) -> str | None:
    """Their profile name -> our profile id, by resolution words."""
    low = name.lower()
    for words, target in (
        (("2160", "uhd", "ultra", "4k"), "2160p"),
        (("1080",), "1080p"),
        (("720",), "720p"),
        (("480", "sd"), "480p"),
        (("any",), "Any"),
    ):
        # a whole word: "720" and "720p", not part of "17200"
        hit = any(re.search(rf"(?<![a-z0-9]){w}p?(?![a-z0-9])", low) for w in words)
        if hit and target in ours:
            return ours[target]
    return ours.get("1080p") or next(iter(ours.values()), None)


def translate(path: str, mappings: list[dict[str, str]]) -> str:
    """Their path as Fetchbox sees it (longest ``from`` prefix wins)."""
    norm = path.replace("\\", "/")
    for m in sorted(mappings, key=lambda m: len(m["from"]), reverse=True):
        src = m["from"].replace("\\", "/").rstrip("/")
        if norm == src or norm.startswith(src + "/"):
            return m["to"].rstrip("/\\") + norm[len(src) :]
    return path


def _rel(root: str, path: str) -> str | None:
    """``path`` relative to ``root`` (both as Fetchbox sees them), or None if outside."""
    r, p = PurePath(root.replace("\\", "/")), PurePath(path.replace("\\", "/"))
    try:
        return p.relative_to(r).as_posix()
    except ValueError:
        return None


class Migrator:
    def __init__(
        self,
        db: Database,
        jobs: JobRunner,
        *,
        storage: StorageService,
        media: MediaService,
        profiles: ProfilesService,
        library: LibraryService,
    ) -> None:
        self.db = db
        self.jobs = jobs
        self.storage = storage
        self.media = media
        self.profiles = profiles
        self.library = library
        self.transport: Any = None  # tests: a fake Radarr / Sonarr
        self._creds: dict[str, tuple[Kind, str, str]] = {}

    def register_jobs(self) -> None:
        @self.jobs.handler(SCAN_JOB, queue="net", max_attempts=1)
        async def scan(ctx: JobContext) -> dict[str, Any]:
            return await self.scan(*self._take(ctx.payload["token"]), progress=ctx.progress)

        @self.jobs.handler(APPLY_JOB, queue="net", max_attempts=1)
        async def apply(ctx: JobContext) -> dict[str, Any]:
            p = ctx.payload
            return await self.apply(
                *self._take(p["token"]),
                options=p["options"],
                actor=Actor(type=p["actor"]["type"], id=p["actor"].get("id")),
                progress=ctx.progress,
            )

    def _take(self, token: str) -> tuple[Kind, str, str]:
        creds = self._creds.pop(token, None)
        if creds is None:
            raise ProblemError(
                409, "Conflict", "the connection details expired: start again", slug="conflict"
            )
        return creds

    async def _enqueue(
        self, job_type: str, kind: Kind, url: str, key: str, extra: dict[str, Any]
    ) -> Job:
        # Fail fast on a wrong address or key, before queueing anything.
        client = ArrClient(kind, url, key, transport=self.transport)
        try:
            await client.status()
        except ArrError as exc:
            raise ProblemError(422, "Validation Failed", exc.message, slug="validation") from None
        finally:
            await client.aclose()
        token = secrets.token_urlsafe(16)
        self._creds[token] = (kind, url, key)
        job = await self.jobs.enqueue(
            job_type, {"token": token, "kind": kind, **extra}, dedupe_key=f"{job_type}:{kind}"
        )
        if job is None:
            self._creds.pop(token, None)
            raise ConflictError(f"a {kind.title()} migration step is already running")
        return job

    def _need_tmdb(self) -> None:
        if not self.media.discovery.configured:
            raise ProblemError(
                409, "Conflict",
                "Fetchbox needs a TMDB key first, to look the titles up: add one in Settings",
                slug="metadata-not-configured",
            )  # fmt: skip

    async def start_scan(self, kind: Kind, url: str, key: str) -> Job:
        self._need_tmdb()
        return await self._enqueue(SCAN_JOB, kind, url, key, {})

    async def start_apply(
        self, kind: Kind, url: str, key: str, options: dict[str, Any], actor: Actor
    ) -> Job:
        self._need_tmdb()
        return await self._enqueue(
            APPLY_JOB,
            kind,
            url,
            key,
            {"options": options, "actor": {"type": actor.type, "id": actor.id}},
        )

    # ---- scan ---------------------------------------------------------------------------------

    async def scan(self, kind: Kind, url: str, key: str, *, progress: Any = None) -> dict[str, Any]:
        client = ArrClient(kind, url, key, transport=self.transport)
        try:
            status = await client.status()
            titles = await client.titles()
            roots = await client.root_folders()
            profiles = await client.quality_profiles()
            indexers = await client.indexers()
            clients = await client.download_clients()
        except ArrError as exc:
            raise ProblemError(422, "Validation Failed", exc.message, slug="validation") from None
        finally:
            await client.aclose()
        ours = {p.name: p.id for p in await self.profiles.list_profiles() if p.media == "video"}
        known = {
            t.tmdb_id
            for t in await self.media.all_titles()
            if t.tmdb_id and t.kind == ("movie" if kind == "radarr" else "series")
        }
        items = []
        for t in titles:
            if kind == "radarr":
                files = 1 if t.get("hasFile") else 0
            else:
                files = int((t.get("statistics") or {}).get("episodeFileCount") or 0)
            items.append(
                {
                    "arr_id": t["id"],
                    "tmdb_id": t.get("tmdbId") or None,
                    "tvdb_id": t.get("tvdbId"),
                    "name": t.get("title"),
                    "year": t.get("year"),
                    "monitored": bool(t.get("monitored")),
                    "profile": t.get("qualityProfileId"),
                    "path": t.get("path"),
                    "files": files,
                    "in_fetchbox": (t.get("tmdbId") or None) in known,
                }
            )
        return {
            "kind": kind,
            "app_version": status.get("version"),
            "titles": items,
            "root_folders": [
                {
                    "path": r["path"],
                    "titles": sum(
                        1 for t in titles if _rel(r["path"], t.get("path") or "") is not None
                    ),
                }
                for r in roots
            ],
            "profiles": [
                {"id": p["id"], "name": p["name"], "maps_to": guess_profile(p["name"], ours)}
                for p in profiles
            ],
            "indexers": [
                {
                    "name": i.get("name"),
                    "implementation": i.get("implementation"),
                    "url": next(
                        (f.get("value") for f in i.get("fields", []) if f.get("name") == "baseUrl"),
                        None,
                    ),
                }
                for i in indexers
            ],
            "download_clients": [
                {
                    "name": c.get("name"),
                    "implementation": c.get("implementation"),
                    "host": next(
                        (f.get("value") for f in c.get("fields", []) if f.get("name") == "host"),
                        None,
                    ),
                }
                for c in clients
            ],
            "counts": {
                "titles": len(items),
                "with_files": sum(1 for i in items if i["files"]),
                "missing_tmdb": sum(1 for i in items if not i["tmdb_id"]),
            },
        }

    # ---- apply --------------------------------------------------------------------------------

    async def apply(
        self,
        kind: Kind,
        url: str,
        key: str,
        *,
        options: dict[str, Any],
        actor: Actor,
        progress: Any = None,
    ) -> dict[str, Any]:
        """options: {path_map: [{from, to}], profile_map: {their id: our id},
        only: [their title ids] (null: all)}"""
        media_kind = "movie" if kind == "radarr" else "series"
        mappings = options.get("path_map") or []
        profile_map = {int(k): v for k, v in (options.get("profile_map") or {}).items()}
        only = set(options.get("only") or []) or None
        client = ArrClient(kind, url, key, transport=self.transport)
        added, files, skipped = 0, 0, []
        try:
            titles = await client.titles()
            roots = await client.root_folders()
            dests = await self._destinations(kind, media_kind, [r["path"] for r in roots], mappings)
            ours = {p.name: p.id for p in await self.profiles.list_profiles() if p.media == "video"}
            theirs = {p["id"]: p["name"] for p in await client.quality_profiles()}
            later = utcnow() + FIRST_SEARCH_AFTER
            todo = [t for t in titles if only is None or t["id"] in only]
            for n, t in enumerate(todo, 1):
                name = t.get("title") or "?"
                if not t.get("tmdbId"):
                    skipped.append({"name": name, "reason": "no TMDB id"})
                    continue
                profile = profile_map.get(t.get("qualityProfileId") or -1) or guess_profile(
                    theirs.get(t.get("qualityProfileId") or -1, ""), ours
                )
                local_path = translate(t.get("path") or "", mappings)
                dest = next(
                    (
                        d
                        for root, d in sorted(dests.items(), key=lambda x: -len(x[0]))
                        if _rel(root, local_path) is not None
                    ),
                    None,
                )
                try:
                    title, new = await self._title(kind, t, profile, later, actor)
                    added += int(new)
                    if dest is not None:
                        files += await self._files(client, kind, t, title, dest, local_path)
                    else:
                        skipped.append(
                            {
                                "name": name,
                                "reason": "its folder isn't under a root folder Fetchbox can see",
                            }
                        )
                except (ProblemError, ArrError) as exc:
                    msg = exc.problem.detail if isinstance(exc, ProblemError) else exc.message
                    skipped.append({"name": name, "reason": msg or "failed"})
                if progress:
                    await progress(n / max(len(todo), 1), f"{n} of {len(todo)}")
        finally:
            await client.aclose()
        return {
            "added": added,
            "files": files,
            "skipped": skipped,
            "library_folders": [d.name for d in dests.values()],
        }

    async def _destinations(
        self, kind: str, media_kind: str, roots: list[str], mappings: list[dict[str, str]]
    ) -> dict[str, Any]:
        """Their root folders as Fetchbox library folders (existing ones reused)."""
        out: dict[str, Any] = {}
        existing = await self.storage.destinations(media_kind)
        for root in roots:
            local = translate(root, mappings).rstrip("/\\")
            match = None
            for d in existing:
                if await asyncio.to_thread(_same_folder, d.path, local):
                    match = d
                    break
            if match is None:
                base = f"{kind.title()} {PurePath(local).name or local}"
                name, n = base, 1
                names = {d.name for d in await self.storage.destinations()}
                while name in names:
                    n += 1
                    name = f"{base} {n}"
                match = await self.storage.create_destination(
                    {"name": name, "path": local, "media_kind": media_kind}, actor=Actor()
                )
            out[local] = match
        return out

    async def _title(
        self, kind: str, t: dict[str, Any], profile: str | None, later: Any, actor: Actor
    ) -> tuple[Title, bool]:
        tmdb = int(t["tmdbId"])
        mk = "movie" if kind == "radarr" else "series"
        for x in await self.media.all_titles():
            if x.kind == mk and x.tmdb_id == tmdb:
                return x, False
        if profile is None:
            raise ProblemError(
                422, "Validation Failed", "no quality profile to use", slug="validation"
            )
        if kind == "radarr":
            title = await self.media.add_movie(
                tmdb,
                profile_id=profile,
                monitored=bool(t.get("monitored")),
                actor=actor,
                search_after=later,
                minimum_availability=AVAILABILITY.get(t.get("minimumAvailability", ""), "released"),
            )
        else:
            title = await self.media.add_series(
                tmdb,
                profile_id=profile,
                monitor_mode="all" if t.get("monitored") else "none",
                actor=actor,
                search_after=later,
            )
        return title, True

    async def _files(
        self,
        client: ArrClient,
        kind: str,
        t: dict[str, Any],
        title: Title,
        dest: Any,
        local_path: str,
    ) -> int:
        if kind == "radarr":
            f = t.get("movieFile") or {}
            if not f.get("relativePath"):
                return 0
            rel = _rel(dest.path, f"{local_path}/{f['relativePath']}")
            wanted = (await self.media.wanted_for([title.id])).get(title.id, [])
            return int(bool(rel and await self.library.adopt(title, dest, rel, wanted)))
        eps = await client.episodes(int(t["id"]))
        efiles = {f["id"]: f for f in await client.episode_files(int(t["id"]))}
        rows = await self.media.episode_items(title.id)
        by_key = {(n, ep.number): w for n, ep, w in rows}
        # Sonarr's per-episode monitoring, copied
        want = {
            by_key[(e["seasonNumber"], e["episodeNumber"])].id: bool(e.get("monitored"))
            for e in eps
            if (e["seasonNumber"], e["episodeNumber"]) in by_key
        }
        async with self.db.sessionmaker() as s:
            for on in (True, False):
                ids = [i for i, m in want.items() if m is on]
                if ids:
                    await s.execute(
                        update(WantedItem)
                        .where(WantedItem.id.in_(ids))
                        .values(monitored=on)
                        .execution_options(synchronize_session=False)
                    )
            await s.commit()
        count = 0
        for fid, f in efiles.items():
            keys = [
                (e["seasonNumber"], e["episodeNumber"])
                for e in eps
                if e.get("episodeFileId") == fid
            ]
            items = [by_key[k] for k in keys if k in by_key]
            rel = _rel(dest.path, f"{local_path}/{f['relativePath']}")
            if items and rel and await self.library.adopt(title, dest, rel, items):
                count += 1
        return count
