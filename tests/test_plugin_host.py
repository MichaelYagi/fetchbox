"""The plugin host against a real running Fetchbox: an add-on's handlers run as jobs for
real events, its schedules and actions run through the job list, a media kind joins the
list of kinds, and unloading undoes it all."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

import httpx
import pytest

from fetchbox.core import kinds
from fetchbox.plugin.host import PluginHost, PluginLoadError
from fetchbox.plugin.services import Services
from fetchbox.providers.download_clients.builtin import BuiltinClient
from fetchbox_plugin import (
    Addon,
    Field,
    Hit,
    Item,
    MediaKind,
    Naming,
    Parsed,
    Plugin,
    PublicEvent,
    Qualities,
    Settings,
    TitleDetails,
    action,
    every,
    on,
)
from tests.conftest import Running
from tests.pipeline import Fakes, _movie
from tests.test_library_api import _acquire, fast, made, seed  # noqa: F401
from tests.torrents import Made, until

SEEN: list[tuple[str, Any]] = []


class Greeting(Settings):
    word: str = Field("Hello", title="Word")


class Watcher(Addon):
    id = "watcher"
    name = "Watcher"
    applies_to: ClassVar[set[str]] = {"movie"}
    settings = Greeting

    @on("title.added")
    async def added(self, ctx: Any, event: PublicEvent) -> None:
        await ctx.store.set("last", event.title_id)  # before SEEN: the test reads it then
        SEEN.append(("added", (event.title_id, event.kind, event.data.get("name"))))

    @on("file.imported")
    async def imported(self, ctx: Any, event: PublicEvent) -> None:
        f = event.file
        assert f is not None
        await ctx.files.add_sidecar(f, "hello.txt", f"{self.settings.word}\n".encode())
        SEEN.append(("imported", f))

    @every(hours=6)
    async def tidy(self, ctx: Any) -> dict[str, Any]:
        SEEN.append(("tidy", None))
        return {"tidied": 1}

    @action("Say hello", on="title")
    async def hello(self, ctx: Any, item: Any) -> None:
        SEEN.append(("hello", item))


class ShowsOnly(Addon):
    id = "shows"
    name = "Shows only"
    applies_to: ClassVar[set[str]] = {"series"}

    @on("title.added")
    async def added(self, ctx: Any, event: PublicEvent) -> None:
        SEEN.append(("shows", event.title_id))


class Broken(Addon):
    id = "broken"
    name = "Broken"

    @on("title.added")
    async def added(self, ctx: Any, event: PublicEvent) -> None:
        SEEN.append(("broken", None))
        raise RuntimeError("this add-on always fails")


class Comics(MediaKind):
    id = "comics"
    name = "Comics"
    levels = ("series", "issue")
    file_types = frozenset({".cbz"})
    qualities = Qualities(["CBZ"])
    naming = Naming("{series} #{issue}", tokens={"series", "issue"})

    async def search(self, ctx: Any, text: str) -> list[Hit]:
        return []

    async def title(self, ctx: Any, ref: str) -> TitleDetails:
        return TitleDetails(ref, ref)

    def parse(self, release_name: str) -> Parsed | None:
        return None

    def name_tokens(self, title: TitleDetails, item: Item) -> dict[str, object]:
        return {}


class Hello(Plugin):
    id = "hello"
    name = "Hello"
    contributes: ClassVar[list[type[Any]]] = [Watcher, ShowsOnly, Broken, Comics]


def _host(server: Running, settings: Any, **kw: Any) -> PluginHost:
    st = server.app.state
    services = Services(library=st.library, media=st.media, jobs=st.fb.jobs,
                        notifications=st.notifications, autosearch=st.autosearch)  # fmt: skip
    return PluginHost(db=st.fb.db, bus=st.fb.bus, scheduler=st.fb.scheduler, services=services,
                      data_dir=settings.data_dir, **kw)  # fmt: skip


@pytest.fixture
async def host(server: Running, settings: Any) -> AsyncIterator[PluginHost]:
    SEEN.clear()
    h = _host(
        server, settings, settings_for=lambda p, part: {"word": "Hi"} if part == "watcher" else {}
    )
    await h.load(Hello)
    yield h
    await h.unload("hello")


async def _seen(kind: str, within: float = 15) -> list[Any]:
    async def got() -> bool:
        return any(k == kind for k, _ in SEEN)

    await until(got, within=within)
    return [v for k, v in SEEN if k == kind]


async def test_an_event_handler_runs_as_a_job(
    c: httpx.AsyncClient, host: PluginHost, server: Running
) -> None:
    movie = await _movie(c)
    [(title_id, kind, name)] = await _seen("added")
    assert (title_id, kind, name) == (movie["id"], "movie", "Dune")
    assert await host.loaded["hello"].ctx.store.get("last") == movie["id"]
    assert [k for k, _ in SEEN].count("shows") == 0  # applies only to series

    async def succeeded() -> bool:  # the handler ran; the job records itself just after
        jobs = (await c.get("/jobs", params={"limit": 100})).json()
        items = jobs.get("items", jobs)
        return any(j["type"] == "plugin.hello.watcher.added" and j["status"] == "succeeded"
                   for j in items)  # fmt: skip

    await until(succeeded, within=15)


async def test_a_failing_handler_fails_alone(c: httpx.AsyncClient, host: PluginHost) -> None:
    await _movie(c)
    await _seen("added")
    await _seen("broken")

    async def failed() -> bool:
        items = (await c.get("/jobs", params={"limit": 100})).json()
        items = items.get("items", items)
        return any(j["type"] == "plugin.hello.broken.added" and j["status"] in ("failed", "pending")
                   and "always fails" in (j.get("error") or "") for j in items)  # fmt: skip

    await until(failed, within=15)
    assert len(await _seen("added")) == 1  # the other add-on's handler ran, once


async def test_a_schedule_and_an_action(c: httpx.AsyncClient, host: PluginHost) -> None:
    got = host.loaded["hello"]
    [name] = got.schedules
    assert name == "plugin.hello.watcher.tidy"
    scheds = {s["name"]: s for s in (await c.get("/automation/schedules")).json()}
    assert scheds[name]["description"] == "Hello: tidy"
    assert scheds[name]["interval_seconds"] == 6 * 3600
    assert (await c.post(f"/automation/schedules/{name}/run")).status_code in (200, 202)
    assert await _seen("tidy") == [None]

    job_id = await host.run_action("hello", "watcher", "hello", "title-1")
    assert job_id and await _seen("hello") == ["title-1"]
    with pytest.raises(LookupError):
        await host.run_action("hello", "watcher", "nope", "x")


async def test_a_media_kind_joins_the_list(host: PluginHost) -> None:
    k = kinds.get("comics")
    assert (k.plugin, k.label, k.levels, k.file_exts) == (
        "hello",
        "Comics",
        ("series", "issue"),
        frozenset({"cbz"}),
    )
    assert k.category == "fetchbox-comics" and not k.core
    assert kinds.keys() == (*kinds.CORE_KEYS, "comics")


async def test_unloading_undoes_it_all(
    c: httpx.AsyncClient, server: Running, settings: Any
) -> None:
    SEEN.clear()
    h = _host(server, settings)
    await h.load(Hello)
    await h.unload("hello")
    assert kinds.keys() == kinds.CORE_KEYS
    assert "plugin.hello.watcher.tidy" not in server.app.state.fb.scheduler.definitions
    await _movie(c)
    await until(lambda: _title_added(c), within=10)
    assert SEEN == []  # no handler ran, nothing was queued
    jobs = (await c.get("/jobs", params={"limit": 100})).json()
    assert not any(j["type"].startswith("plugin.") for j in jobs.get("items", jobs))
    await h.load(Hello)  # and it loads again
    await h.unload("hello")


async def _title_added(c: httpx.AsyncClient) -> bool:
    return bool((await c.get("/titles")).json())


async def test_what_cant_be_loaded(server: Running, settings: Any, host: PluginHost) -> None:
    class Future(Plugin):
        id = "future"
        name = "Future"
        fetchbox_api = 2

    class BadId(Plugin):
        id = "Bad Id"
        name = "Bad"

    class Twin(Plugin):  # a second plugin with the same media kind
        id = "twin"
        name = "Twin"
        contributes: ClassVar[list[type[Any]]] = [Comics]

    with pytest.raises(PluginLoadError, match="plugin API 2; this Fetchbox supports API 1"):
        await host.load(Future)
    with pytest.raises(PluginLoadError, match="plugin id"):
        await host.load(BadId)
    with pytest.raises(PluginLoadError, match="already loaded"):
        await host.load(Hello)
    with pytest.raises(PluginLoadError, match="already a kind"):
        await host.load(Twin)
    assert "twin" not in host.loaded and kinds.get("comics").plugin == "hello"


async def test_an_import_runs_the_addon_with_its_settings(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,  # noqa: F811
    seed: BuiltinClient,  # noqa: F811
    host: PluginHost,
) -> None:
    movie = await _acquire(c, fakes, server, made, seed)
    [file_id] = await _seen("imported", within=30)
    [f] = (await c.get(f"/titles/{movie['id']}/files")).json()
    assert file_id == f["id"]
    path = await host.loaded["hello"].ctx.files.path(file_id)
    assert path.with_suffix(".hello.txt").read_text() == "Hi\n"  # the stored settings


async def test_settings_that_dont_validate_say_which(server: Running, settings: Any) -> None:
    class Needs(Settings):
        api_key: str = Field(title="Key")

    class Keyed(Addon):
        id = "keyed"
        name = "Keyed"
        settings = Needs

        @on("title.added")
        async def added(self, ctx: Any, event: PublicEvent) -> None: ...

    class KeyedPlugin(Plugin):
        id = "keyed"
        name = "Keyed"
        contributes: ClassVar[list[type[Any]]] = [Keyed]

    h = _host(server, settings)
    with pytest.raises(
        PluginLoadError,
        match=r"keyed: its settings aren't complete or valid \(api_key: Field required\)",
    ):
        await h.load(KeyedPlugin)
    assert "keyed" not in h.loaded
