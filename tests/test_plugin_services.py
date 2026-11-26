"""ctx.files, ctx.library, ctx.jobs and ctx.notify against a real running Fetchbox: a plugin
sees the library as the API does and acts through the same services."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from fetchbox.core.jobs import JobContext
from fetchbox.modules.notifications.models import NotificationTarget
from fetchbox.plugin.context import PluginContext
from fetchbox.plugin.services import PLUGIN_MESSAGE, PluginRefused, Services
from fetchbox.providers.download_clients.builtin import BuiltinClient
from fetchbox_plugin import NotFound
from tests.conftest import Running
from tests.pipeline import Fakes
from tests.test_library_api import EXPECTED, _acquire, fast, made, seed  # noqa: F401
from tests.test_notifications import Receiver, receiver  # noqa: F401
from tests.torrents import Made, until

SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"


def _services(server: Running) -> Services:
    st = server.app.state
    return Services(
        library=st.library,
        media=st.media,
        jobs=st.fb.jobs,
        notifications=st.notifications,
        autosearch=st.autosearch,
    )


@pytest.fixture
async def ctxs(server: Running, settings: Any) -> AsyncIterator[dict[str, PluginContext]]:
    out = {
        p: PluginContext(p, db=server.app.state.fb.db, data_dir=settings.data_dir,
                         services=_services(server))
        for p in ("subtitles", "other")
    }  # fmt: skip
    yield out
    for c in out.values():
        await c.aclose()


async def test_files_and_library_as_a_plugin_sees_them(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,  # noqa: F811
    seed: BuiltinClient,  # noqa: F811
    settings: Any,
    ctxs: dict[str, PluginContext],
) -> None:
    ctx, other = ctxs["subtitles"], ctxs["other"]
    movie = await _acquire(c, fakes, server, made, seed)
    [api_file] = (await c.get(f"/titles/{movie['id']}/files")).json()
    root = settings.downloads_dir / "Movies"

    # library: the same titles and files the API shows
    [t] = await ctx.library.titles()
    assert (t.id, t.kind, t.name, t.year) == (movie["id"], "movie", "Dune", 2021)
    assert t.ids["tmdb_id"] == movie["tmdb_id"] and t.monitored
    assert await ctx.library.titles(kind="series") == []
    assert await ctx.library.title(movie["id"]) == t
    [f] = await ctx.library.files(movie["id"])
    assert f.id == api_file["id"] and f.title_id == movie["id"]
    assert f.path == root / EXPECTED and f.path.is_file() and f.size == api_file["size"]
    assert f.quality == api_file["quality"]["name"]
    assert f.item_ids == (movie["wanted"][0]["id"],)
    assert await ctx.library.file(f.id) == f
    with pytest.raises(NotFound):  # the API's, which a plugin can catch
        await ctx.library.title("nope")

    # files: a sidecar beside it, tied to it, the plugin's own
    assert await ctx.files.path(f.id) == f.path
    sc = await ctx.files.add_sidecar(f.id, "en.srt", SRT, meta={"lang": "en"})
    assert sc.path == f.path.with_suffix(".en.srt") and sc.path.read_bytes() == SRT
    assert (sc.plugin, sc.meta) == ("subtitles", {"lang": "en"})
    await ctx.files.add_sidecar(f.id, "nfo", b"<movie/>")
    assert [s.suffix for s in await ctx.files.sidecars(f.id, suffix=".srt")] == ["en.srt"]
    assert len(await other.files.sidecars(f.id)) == 2  # every plugin's are visible
    with pytest.raises(PluginRefused):
        await other.files.add_sidecar(f.id, "en.srt", b"mine now")
    with pytest.raises(PluginRefused):
        await other.files.remove_sidecar(f.id, "en.srt")
    assert sc.path.read_bytes() == SRT
    assert await ctx.files.remove_sidecar(f.id, "en.srt")
    assert not sc.path.exists() and not await ctx.files.remove_sidecar(f.id, "en.srt")

    # library: unwanted and wanted again, as the title page does it
    item = f.item_ids[0]
    await ctx.library.wanted(item, monitored=False)
    got = (await c.get(f"/titles/{movie['id']}")).json()
    assert got["wanted"][0]["monitored"] is False and got["monitor_mode"] == "none"
    assert not (await ctx.library.title(movie["id"])).monitored
    await ctx.library.wanted(item)
    assert (await c.get(f"/titles/{movie['id']}")).json()["wanted"][0]["monitored"] is True
    history = (await c.get("/activity")).json()
    assert any("subtitles plugin" in str(e) for e in history.get("items", history))


async def test_a_plugins_jobs_run_in_fetchbox(
    server: Running, ctxs: dict[str, PluginContext], c: httpx.AsyncClient
) -> None:
    ctx = ctxs["subtitles"]
    ran: list[dict[str, Any]] = []

    @ctx.jobs.job("sync")
    async def sync(job: JobContext) -> dict[str, Any]:
        await job.progress(0.5, "halfway")
        ran.append(job.payload)
        return {"synced": len(ran)}

    assert sync is not None
    job_id = await ctx.jobs.enqueue("sync", {"since": "2026-11-01"}, dedupe_key="all")
    assert job_id is not None

    async def done() -> bool:
        return (await c.get(f"/jobs/{job_id}")).json()["status"] == "succeeded"

    await until(done, within=15)
    job = (await c.get(f"/jobs/{job_id}")).json()
    assert job["type"] == "plugin.subtitles.sync" and job["result"] == {"synced": 1}
    assert ran == [{"since": "2026-11-01"}]
    with pytest.raises(ValueError, match="already registered"):
        ctx.jobs.job("sync")(sync)  # one handler per job name


async def test_notify_reaches_targets_that_chose_plugin_messages(
    server: Running,
    ctxs: dict[str, PluginContext],
    c: httpx.AsyncClient,
    receiver: Receiver,  # noqa: F811
) -> None:
    ctx = ctxs["subtitles"]
    r = await c.post("/notifications/targets", json={"kind": "webhook", "name": "Hook",
                     "settings": {"url": receiver.url}})  # fmt: skip
    assert r.status_code == 201, r.text
    assert await ctx.notify("Subtitles found", "Dune (2021): en") == 0  # not chosen: nobody

    async with server.app.state.fb.db.sessionmaker() as s:  # (chosen in Settings, phase 4)
        await s.execute(update(NotificationTarget).values(events=[PLUGIN_MESSAGE]))
        await s.commit()
    assert await ctx.notify("Subtitles found", "Dune (2021): en", level="success") == 1

    async def got() -> bool:
        return len(receiver.got) == 1

    await until(got, within=10)
    headers, body = receiver.got[0]
    assert body["title"] == "Subtitles found" and body["body"] == "Dune (2021): en"
    assert body["level"] == "success" and headers["X-Fetchbox-Event"] == PLUGIN_MESSAGE


async def test_without_services_a_context_says_so(tmp_path: Path, server: Running) -> None:
    ctx = PluginContext("lonely", db=server.app.state.fb.db, data_dir=tmp_path)
    try:
        with pytest.raises(RuntimeError, match="no Fetchbox services"):
            _ = ctx.files
    finally:
        await ctx.aclose()
