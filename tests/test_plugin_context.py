"""The plugin context: what each service of ``ctx`` gives a plugin, and its limits."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs

from fetchbox import __version__, migrations
from fetchbox.core.db import Database
from fetchbox.modules.plugins.store import StoreError
from fetchbox.plugin.context import (
    PluginCache,
    PluginContext,
    PluginHttp,
    PluginIdError,
    check_id,
)


@pytest.fixture
def url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'p.db').as_posix()}"
    migrations.upgrade(url)  # (runs its own event loop: not inside a test's)
    return url


@pytest.fixture
async def db(url: str) -> AsyncIterator[Database]:
    d = Database(url)
    yield d
    await d.dispose()


@pytest.fixture
async def ctx(db: Database, tmp_path: Path) -> AsyncIterator[PluginContext]:
    c = PluginContext("comics", db=db, data_dir=tmp_path / "data")
    yield c
    await c.aclose()


@pytest.mark.parametrize("good", ["comics", "open-subtitles", "fb_artwork", "a1"])
def test_plugin_ids(good: str) -> None:
    assert check_id(good) == good


@pytest.mark.parametrize("bad", ["", "c", "Comics", "1comics", "comics!", "a" * 41, "my plugin"])
def test_bad_plugin_ids(bad: str) -> None:
    with pytest.raises(PluginIdError):
        check_id(bad)


# ---- store ------------------------------------------------------------------------------


async def test_store_keeps_values_per_plugin(ctx: PluginContext, db: Database, tmp_path: Path):
    s = ctx.store
    assert await s.get("last_sync") is None
    assert await s.get("last_sync", 0) == 0
    await s.set("last_sync", {"at": "2026-11-26", "count": 3})
    await s.set("seen", [1, 2, 3])
    await s.set("seen", (4, 5))  # replaced; a tuple comes back as a list
    assert await s.get("last_sync") == {"at": "2026-11-26", "count": 3}
    assert await s.get("seen") == [4, 5]

    other = PluginContext("subtitles", db=db, data_dir=tmp_path / "data")
    try:
        assert await other.store.get("seen") is None  # each plugin has its own
        await other.store.set("seen", "theirs")
        assert await s.get("seen") == [4, 5]
        await s.set("a_%", 1)
        await s.set("ab", 2)
        assert await s.keys() == ["a_%", "ab", "last_sync", "seen"]
        assert await s.keys("a_") == ["a_%"]  # a prefix, not a pattern
        assert await s.delete("ab") and not await s.delete("ab")
        assert await s.clear() == 3
        assert await s.keys() == []
        assert await other.store.get("seen") == "theirs"
    finally:
        await other.aclose()


async def test_store_refuses_what_it_cant_keep(ctx: PluginContext) -> None:
    with pytest.raises(StoreError, match="isn't JSON"):
        await ctx.store.set("x", object())
    with pytest.raises(StoreError, match="KB"):
        await ctx.store.set("x", "a" * 70_000)
    with pytest.raises(StoreError, match="characters"):
        await ctx.store.set("", 1)
    with pytest.raises(StoreError, match="characters"):
        await ctx.store.set("k" * 201, 1)
    assert await ctx.store.keys() == []


# ---- folder and log ----------------------------------------------------------------------


async def test_folder_is_the_plugins_own(ctx: PluginContext, tmp_path: Path) -> None:
    where = tmp_path / "data" / "plugins" / "comics"
    assert not where.exists()  # made only when asked for
    assert ctx.folder == where and where.is_dir()


async def test_log_is_tagged_with_the_plugin(ctx: PluginContext) -> None:
    with capture_logs() as logs:
        ctx.log.info("synced", count=3)
    assert logs == [{"event": "synced", "count": 3, "plugin": "comics", "log_level": "info"}]


# ---- http ---------------------------------------------------------------------------------


def _http(handler) -> PluginHttp:  # type: ignore[no-untyped-def]
    return PluginHttp("comics", transport=httpx.MockTransport(handler))


async def test_http_names_the_plugin() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    http = _http(handler)
    try:
        r = await http.get("https://api.example.com/x", params={"q": "saga"})
        assert r.json() == {"ok": True}
        assert seen[0].headers["user-agent"] == f"Fetchbox/{__version__} (plugin comics)"
        assert seen[0].url.params["q"] == "saga"
    finally:
        await http.aclose()


async def test_http_retries_a_get_but_not_a_post() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.method)
        if len(calls) % 3:  # two busy answers, then a real one
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200)

    http = _http(handler)
    try:
        assert (await http.get("https://api.example.com/x")).status_code == 200
        assert calls == ["GET", "GET", "GET"]
        calls.clear()
        assert (await http.post("https://api.example.com/x")).status_code == 503
        assert calls == ["POST"]  # a POST may have done something: never sent twice
    finally:
        await http.aclose()


async def test_http_gives_up_after_its_retries() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "0"})

    http = _http(handler)
    try:
        assert (await http.get("https://api.example.com/x")).status_code == 429
        assert calls == 3
    finally:
        await http.aclose()


async def test_http_doesnt_wait_out_a_long_retry_after() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "3600"})

    http = _http(handler)
    try:
        assert (await http.get("https://api.example.com/x")).status_code == 429
        assert calls == 1  # the plugin decides what to do with an hour's wait
    finally:
        await http.aclose()


async def test_http_keeps_to_a_hosts_rate_limit() -> None:
    http = _http(lambda req: httpx.Response(200))
    http.limit("api.example.com", per_second=20)
    try:
        start = time.monotonic()
        for _ in range(4):
            await http.get("https://API.example.com/x")
        assert time.monotonic() - start >= 0.14  # 3 gaps of 0.05 s
        start = time.monotonic()
        for _ in range(4):
            await http.get("https://other.example.com/x")  # no limit declared
        assert time.monotonic() - start < 0.1
        with pytest.raises(ValueError):
            http.limit("x", per_second=0)
    finally:
        await http.aclose()


# ---- cache --------------------------------------------------------------------------------


async def test_cache_keeps_answers_for_a_while() -> None:
    cache = PluginCache(max_entries=2)
    fetched = 0

    async def fetch() -> str:
        nonlocal fetched
        fetched += 1
        return f"answer {fetched}"

    assert await cache.remember("q", 60, fetch) == "answer 1"
    assert await cache.remember("q", 60, fetch) == "answer 1"
    assert fetched == 1
    cache.set("short", "x", 0.01)
    await asyncio.sleep(0.02)
    assert cache.get("short") is None  # expired
    cache.set("a", 1, 60)
    cache.set("b", 2, 60)  # over max_entries: the least recently used ("q") goes
    assert cache.get("q") is None and cache.get("a") == 1
    cache.set("none", None, 60)
    assert await cache.remember("none", 60, fetch) is None  # a kept None is an answer
    cache.clear()
    assert cache.get("a") is None
