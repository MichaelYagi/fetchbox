"""Direct downloads: one file over HTTP(S), for releases that aren't torrents.

The built-in counterpart of the torrent engine for sources like Internet Archive, whose
files are better fetched from their web seed (see ``download: webseed`` in indexer
definitions). Files are written to ``<save path>/<name>/<file>.part`` and renamed when
complete; an interrupted download resumes with a Range request, also across restarts
(jobs are kept in ``<state dir>/direct.json``). The ref is the URL's SHA-1, so the same
file isn't fetched twice. A finished file reports as ``seeding`` (complete), like a
finished torrent; there is nothing to upload.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import unquote, urlsplit

import httpx
import structlog
from pydantic import BaseModel, Field

from fetchbox.core.providers import TestResult, registry
from fetchbox.providers.download_clients import (
    AddOptions,
    ClientFile,
    ClientRef,
    ClientTorrent,
    DownloadClientError,
    DownloadClientProvider,
    TorrentState,
    TransferLimits,
)
from fetchbox.providers.indexers import TorrentSource

log = structlog.get_logger(__name__)

CHUNK = 256 * 1024
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
RETRIES = 5  # network errors in a row before the download is marked failed
PARTIAL = ".part"


class DirectSettings(BaseModel):
    download_limit: int | None = Field(default=None, ge=0, description="Bytes/s; empty = none")


@dataclass
class _Job:
    ref: str
    url: str
    save_path: str
    name: str
    filename: str
    size: int | None = None
    done: int = 0
    state: TorrentState = "paused"
    error: str | None = None
    finished_at: float | None = None
    limit: int | None = None
    # runtime only
    rate: float = field(default=0.0, compare=False)

    @property
    def path(self) -> Path:
        return Path(self.save_path) / self.name / self.filename

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("rate")
        return d


def _prepare(part: Path) -> int:
    """Create the folder; bytes already downloaded."""
    part.parent.mkdir(parents=True, exist_ok=True)
    return part.stat().st_size if part.exists() else 0


def _size(p: Path) -> int:
    return p.stat().st_size


def _filename(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "download"
    return "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name)


class DirectClient(DownloadClientProvider):
    kind: ClassVar[str] = "direct"
    protocol: ClassVar[Literal["torrent", "usenet", "direct"]] = "direct"
    Settings: ClassVar[type[BaseModel]] = DirectSettings

    def __init__(
        self,
        settings: BaseModel,
        *,
        state_dir: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(settings)
        assert isinstance(settings, DirectSettings)
        self.cfg = settings
        self.state_file = (state_dir / "direct.json") if state_dir else None
        self._http = httpx.AsyncClient(
            timeout=TIMEOUT,
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": "Fetchbox"},
        )
        self.jobs: dict[str, _Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # ---- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        if self.state_file and self.state_file.is_file():
            for d in json.loads(self.state_file.read_text(encoding="utf-8")):
                job = _Job(**d)
                self.jobs[job.ref] = job
        for job in self.jobs.values():
            if job.state in ("downloading", "metadata"):
                self._run(job)

    async def stop(self) -> None:
        for ref in list(self._tasks):
            await self._cancel(ref)
        self._save()
        await self._http.aclose()

    def _save(self) -> None:
        if self.state_file is None:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps([j.to_json() for j in self.jobs.values()]), encoding="utf-8")
        os.replace(tmp, self.state_file)

    async def test(self) -> TestResult:
        active = sum(j.state == "downloading" for j in self.jobs.values())
        return TestResult(ok=True, message=f"ready ({active} downloading)")

    # ---- the download loop --------------------------------------------------------------------

    def _run(self, job: _Job) -> None:
        if (t := self._tasks.get(job.ref)) is not None and not t.done():
            return
        job.state, job.error = "downloading", None
        self._tasks[job.ref] = asyncio.create_task(self._download(job), name=f"direct-{job.ref}")

    async def _cancel(self, ref: str) -> None:
        if (t := self._tasks.pop(ref, None)) is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _download(self, job: _Job) -> None:
        failures = 0
        part = job.path.with_name(job.path.name + PARTIAL)
        while True:
            try:
                await self._fetch(job, part)
                break
            except DownloadClientError as exc:
                job.state, job.error = "error", exc.message
                break
            except (httpx.HTTPError, OSError) as exc:
                failures += 1
                if failures >= RETRIES:
                    job.state, job.error = "error", f"download failed: {type(exc).__name__}"
                    break
                await asyncio.sleep(min(2**failures, 30))
        job.rate = 0.0
        self._save()

    async def _fetch(self, job: _Job, part: Path) -> None:
        have = await asyncio.to_thread(_prepare, part)
        headers = {"Range": f"bytes={have}-"} if have else {}
        async with self._http.stream("GET", job.url, headers=headers) as r:
            if r.status_code == 416 and job.size and have >= job.size:
                pass  # already complete
            elif r.status_code in (403, 404, 410):
                raise DownloadClientError(
                    "not_found", f"the server says {r.status_code} for {job.url}"
                )
            elif r.status_code >= 400:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            else:
                if r.status_code == 200 and have:
                    have = 0  # no range support: start over
                length = r.headers.get("content-length")
                if length and length.isdigit():
                    total = have + int(length)
                    if job.size and total != job.size:
                        log.warning(
                            "direct.size_differs", ref=job.ref, expected=job.size, got=total
                        )
                    job.size = total
                job.done = have
                f = await asyncio.to_thread(open, part, "ab" if have else "wb")
                try:
                    window_start, window_bytes = time.monotonic(), 0
                    async for chunk in r.aiter_bytes(CHUNK):
                        await asyncio.to_thread(f.write, chunk)
                        job.done += len(chunk)
                        window_bytes += len(chunk)
                        elapsed = time.monotonic() - window_start
                        if elapsed >= 1.0:
                            job.rate = window_bytes / elapsed
                            window_start, window_bytes = time.monotonic(), 0
                        limit = job.limit or self.cfg.download_limit
                        if limit and elapsed > 0 and window_bytes / max(elapsed, 1e-3) > limit:
                            await asyncio.sleep(window_bytes / limit - elapsed)
                finally:
                    await asyncio.to_thread(f.close)
        size = await asyncio.to_thread(_size, part)
        if job.size is not None and size != job.size:
            raise httpx.TransportError(f"got {size} of {job.size} bytes")  # resume and retry
        await asyncio.to_thread(os.replace, part, job.path)
        job.size = job.done = size
        job.state, job.finished_at = "seeding", time.time()
        log.info("direct.finished", ref=job.ref, path=str(job.path), size=size)

    # ---- DownloadClientProvider ---------------------------------------------------------------

    def _job(self, ref: ClientRef) -> _Job:
        job = self.jobs.get(ref)
        if job is None:
            raise DownloadClientError("not_found", f"no direct download {ref}")
        return job

    async def add(self, src: TorrentSource, opts: AddOptions) -> ClientRef:
        if not opts.save_path:
            raise DownloadClientError("rejected", "the direct client needs a save path")
        if not src.url or urlsplit(src.url).scheme not in ("http", "https"):
            raise DownloadClientError("invalid_torrent", "the direct client takes HTTP(S) files")
        ref = hashlib.sha1(src.url.encode(), usedforsecurity=False).hexdigest()
        if ref in self.jobs:
            return ref
        filename = _filename(src.url)
        name = (src.name or Path(filename).stem).replace("/", "_").replace("\\", "_")
        job = _Job(
            ref=ref,
            url=src.url,
            save_path=opts.save_path,
            name=name,
            filename=filename,
            size=src.size,
            limit=opts.download_limit,
        )
        self.jobs[ref] = job
        if not opts.paused:
            self._run(job)
        self._save()
        return ref

    def _status(self, job: _Job) -> ClientTorrent:
        size = job.size
        progress = 1.0 if job.state == "seeding" else (job.done / size if size else 0.0)
        running = job.ref in self._tasks and not self._tasks[job.ref].done()
        eta = int((size - job.done) / job.rate) if size and job.rate > 0 else None
        return ClientTorrent(
            ref=job.ref,
            name=job.name,
            state=job.state,
            progress=min(progress, 1.0),
            size=size,
            downloaded=job.done,
            uploaded=0,
            download_rate=int(job.rate),
            upload_rate=0,
            seeds=1 if running else 0,  # the server, while connected
            peers=0,
            ratio=0.0,
            seed_seconds=int(time.time() - job.finished_at) if job.finished_at else 0,
            save_path=job.save_path,
            error=job.error,
            eta_seconds=eta,
        )

    async def status(self, refs: list[ClientRef] | None = None) -> list[ClientTorrent]:
        jobs = (
            self.jobs.values() if refs is None else [self.jobs[r] for r in refs if r in self.jobs]
        )
        return [self._status(j) for j in jobs]

    async def files(self, ref: ClientRef) -> list[ClientFile]:
        job = self._job(ref)
        st = self._status(job)
        return [
            ClientFile(path=f"{job.name}/{job.filename}", size=job.size or 0, progress=st.progress)
        ]

    async def set_limits(self, ref: ClientRef | None, limits: TransferLimits) -> None:
        if ref is None:
            self.cfg = self.cfg.model_copy(update={"download_limit": limits.download})
        else:
            self._job(ref).limit = limits.download

    async def pause(self, ref: ClientRef) -> None:
        job = self._job(ref)
        if job.state == "seeding":
            return
        await self._cancel(ref)
        job.state, job.rate = "paused", 0.0
        self._save()

    async def resume(self, ref: ClientRef) -> None:
        job = self._job(ref)
        if job.state != "seeding":
            self._run(job)
            self._save()

    async def remove(self, ref: ClientRef, delete_data: bool) -> None:
        job = self._job(ref)
        await self._cancel(ref)
        self.jobs.pop(ref, None)
        if delete_data:

            def rm() -> None:
                for p in (job.path, job.path.with_name(job.path.name + PARTIAL)):
                    p.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    job.path.parent.rmdir()  # only if now empty

            await asyncio.to_thread(rm)
        self._save()

    async def recheck(self, ref: ClientRef) -> None:
        job = self._job(ref)
        ok = await asyncio.to_thread(job.path.is_file)
        if job.state == "seeding" and ok:
            return
        job.state, job.finished_at = "downloading", None
        self._run(job)


registry.register(DirectClient)
