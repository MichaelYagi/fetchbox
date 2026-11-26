"""The test kit's engine: a real Fetchbox, in-process, with plugins loaded. Plugin authors
use it through ``fetchbox_plugin.testing``; everything here is internal.

It never touches the network or the real Downloads folder: the library, the data folder and
the torrent engine are all under a temporary folder, isolated.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from fetchbox.app import create_app
from fetchbox.config import load_settings
from fetchbox.core.db import utcnow
from fetchbox.core.ids import new_id
from fetchbox.plugin.context import PluginContext, PluginHttp
from fetchbox.plugin.host import PluginHost
from fetchbox.plugin.services import PluginLibrary, Services
from fetchbox_plugin import FileInfo, Plugin, PublicEvent

ENV = {
    "FETCHBOX_TORRENT_ISOLATED": "1",  # no DHT, no peers from the internet
    "FETCHBOX_SEED_INDEXERS": "0",  # no real indexers
}
CLEARED = ("FETCHBOX_DATA_DIR", "FETCHBOX_PORT", "FETCHBOX_DB_URL", "FETCHBOX_HOST")
DONE = ("succeeded", "failed", "cancelled")


@dataclass(frozen=True)
class Ran:
    """How one handler's job went."""

    job_type: str  # plugin.<id>.<add-on>.<method>
    status: str  # succeeded, failed (a failed attempt counts: the kit doesn't wait retries)
    error: str | None
    result: dict[str, Any] | None


class HandlerFailed(AssertionError):
    pass


@contextmanager
def _isolated(root: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in (*ENV, *CLEARED, "FETCHBOX_DOWNLOADS_ROOT")}
    try:
        for k in CLEARED:
            os.environ.pop(k, None)
        os.environ.update(ENV)
        os.environ["FETCHBOX_DOWNLOADS_ROOT"] = str(root / "library")
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class App:
    """A running Fetchbox with plugins loaded."""

    def __init__(self, fastapi: Any, host: PluginHost, client: httpx.AsyncClient, root: Path):
        self.fastapi = fastapi
        self.host = host
        self.client = client  # the API (/api/v1), signed in
        self.root = root
        self.data_dir = root / "data"

    def ctx(self, plugin_id: str) -> PluginContext:
        """The plugin's ctx, to look at what it stored or wrote."""
        return self.host.loaded[plugin_id].ctx

    def fake_http(
        self, plugin_id: str, handler: Callable[[httpx.Request], httpx.Response]
    ) -> list[httpx.Request]:
        """Answer the plugin's ctx.http requests with ``handler`` (a function from request to
        response) instead of the network; the requests it made, to look at."""
        seen: list[httpx.Request] = []

        def record(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return handler(req)

        ctx = self.ctx(plugin_id)
        ctx.http = PluginHttp(plugin_id, transport=httpx.MockTransport(record))
        return seen

    async def emit(
        self,
        type: str,
        *,
        title_id: str | None = None,
        kind: str | None = None,
        file_ids: Sequence[str] = (),
        within: float = 30.0,
        check: bool = True,
        **data: Any,
    ) -> list[Ran]:
        """Fire a public event and run the plugins' handlers for it, as Fetchbox does; wait
        for them. With ``check``, a handler that raised fails the test with its error."""
        event = PublicEvent(
            id=new_id(), type=type, occurred_at=utcnow(), title_id=title_id, kind=kind,
            file_ids=list(file_ids), data=data,
        )  # fmt: skip
        ids = await self.host.dispatch(event)
        ran = [await self._outcome(i, within) for i in ids]
        failed = [r for r in ran if r.status != "succeeded"]
        if check and failed:
            raise HandlerFailed("; ".join(f"{r.job_type}: {r.error}" for r in failed))
        return ran

    async def run(
        self, plugin_id: str, method: str, item: Any = None, *, within: float = 30.0,
        check: bool = True,
    ) -> Ran:  # fmt: skip
        """Run one of a plugin's @every or @action methods now, as Fetchbox does (a job),
        and wait for it: ``await app.run("subtitles", "retry_missing")``."""
        got = self.host.loaded[plugin_id]
        types = [t for t in got.job_types if t.rsplit(".", 1)[-1] == method]
        if not types:
            raise LookupError(f"{plugin_id} has no @every or @action method {method!r}")
        job = await self.host.services.jobs.enqueue(types[0], {"item": item})
        assert job is not None
        ran = await self._outcome(job.id, within)
        if check and ran.status != "succeeded":
            raise HandlerFailed(f"{ran.job_type}: {ran.error}")
        return ran

    async def _outcome(self, job_id: str, within: float) -> Ran:
        jobs = self.host.services.jobs
        deadline = asyncio.get_running_loop().time() + within
        while True:
            job = await jobs.get(job_id)
            assert job is not None
            if job.status in DONE or (job.status == "pending" and job.error):
                status = "succeeded" if job.status == "succeeded" else "failed"
                return Ran(job.type, status, job.error, job.result)
            if asyncio.get_running_loop().time() > deadline:
                return Ran(job.type, "timed out", f"still {job.status} after {within}s", None)
            await asyncio.sleep(0.05)

    async def add_file(
        self,
        name: str = "Dune",
        year: int | None = 2021,
        *,
        kind: str = "movie",
        path: str | None = None,
        size: int = 1024,
        quality: str = "WEBDL-1080p",
        imported: bool = True,
        season: int | None = None,
        episode: int | None = None,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
    ) -> FileInfo:
        """A title with one file in its library folder, as if downloaded and imported (no
        metadata lookup, no download). With ``imported``, ``file.imported`` is fired and
        the plugins' handlers for it have run when this returns. For a series, ``season``
        and ``episode`` make the file that episode's."""
        from fetchbox.modules.library.models import MediaFile, MediaFileItem
        from fetchbox.modules.media.models import Episode, Season, Title, WantedItem
        from fetchbox.modules.profiles.models import QualityProfile

        st = self.fastapi.state
        dest = await st.storage.default_destination(kind)
        folder = f"{name} ({year})" if year else name
        if kind == "series" and season is not None and episode is not None:
            default = f"{folder}/Season {season:02}/{name} - S{season:02}E{episode:02}.mkv"
        else:
            default = f"{folder}/{folder}.mkv"
        rel = path or default
        on_disk = Path(dest.path) / rel
        await asyncio.to_thread(_write, on_disk, size)
        async with st.fb.db.sessionmaker() as s:
            media = "music" if kind == "music" else "video"
            profile = await s.scalar(
                select(QualityProfile.id).where(QualityProfile.media == media).limit(1)
            )
            title = Title(kind=kind, name=name, sort_name=name.lower(), year=year,
                          profile_id=profile, destination_id=dest.id,
                          tmdb_id=tmdb_id, imdb_id=imdb_id)  # fmt: skip
            s.add(title)
            await s.flush()
            ep_id = None
            if kind == "series" and season is not None and episode is not None:
                row = Season(title_id=title.id, number=season)
                s.add(row)
                await s.flush()
                ep = Episode(season_id=row.id, number=episode, name=f"Episode {episode}")
                s.add(ep)
                await s.flush()
                ep_id = ep.id
            item = WantedItem(title_id=title.id, episode_id=ep_id, state="acquired")
            s.add(item)
            f = MediaFile(storage_node_id=dest.node_id, destination_id=dest.id,
                          relative_path=rel, size=size, quality={"name": quality})  # fmt: skip
            s.add(f)
            await s.flush()
            s.add(MediaFileItem(media_file_id=f.id, wanted_item_id=item.id))
            await s.commit()
            title_id, file_id = title.id, f.id
        if imported:
            await self.emit("file.imported", title_id=title_id, kind=kind, file_ids=[file_id],
                            path=rel, quality=quality, destination_id=dest.id)  # fmt: skip
        return await PluginLibrary("test", self.host.services).file(file_id)

    def library_file(self, path: str, kind: str = "movie") -> Path:
        """Where a file is in the default library folder for ``kind``."""
        folder = {"movie": "Movies", "series": "TV", "music": "Music"}.get(kind, kind)
        return self.root / "library" / folder / path


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


@asynccontextmanager
async def start(
    plugins: Sequence[type[Plugin]],
    *,
    root: Path,
    settings: dict[str, dict[str, Any]] | None = None,
) -> AsyncIterator[App]:
    """Start Fetchbox under ``root`` with ``plugins`` loaded. ``settings``: per plugin id,
    its settings (or, per contribution id, each one's)."""
    given = settings or {}

    def settings_for(plugin: str, part: str) -> dict[str, Any]:
        mine = given.get(plugin) or {}
        own = mine.get(part)
        return dict(own) if isinstance(own, dict) else dict(mine)

    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    with _isolated(root):
        keys: list[str] = []
        fastapi = create_app(load_settings(root / "data"), on_bootstrap_key=keys.append)
        async with fastapi.router.lifespan_context(fastapi):
            st = fastapi.state
            services = Services(
                library=st.library,
                media=st.media,
                jobs=st.fb.jobs,
                notifications=st.notifications,
                autosearch=st.autosearch,
            )
            host = PluginHost(db=st.fb.db, bus=st.fb.bus, scheduler=st.fb.scheduler,
                              services=services, data_dir=root / "data",
                              settings_for=settings_for)  # fmt: skip
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fastapi),
                base_url="http://fetchbox/api/v1",
                headers={"Authorization": f"Bearer {keys[0]}"},
            )
            try:
                for p in plugins:
                    await host.load(p)
                yield App(fastapi, host, client, root)
            finally:
                for pid in list(host.loaded):
                    await host.unload(pid)
                await client.aclose()
