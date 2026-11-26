"""The plugin context, ``ctx``: the only way a plugin talks to Fetchbox.

Every method a plugin writes receives one. What's on it is public (API 1); anything a
plugin reaches past it for is internal and may change in any release. Each plugin gets its
own context, so whatever it does is attributed to it in logs.

``http``, ``cache``, ``store``, ``folder`` and ``log`` work on their own; ``files``,
``library``, ``jobs`` and ``notify`` work through Fetchbox's services (see services.py).
``settings`` joins with plugin settings tabs.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import httpx
import structlog

from fetchbox import __version__
from fetchbox.core.db import Database
from fetchbox.modules.plugins.store import PluginStore
from fetchbox.plugin.services import (
    PluginFiles,
    PluginJobs,
    PluginLibrary,
    PluginNotify,
    Services,
)

T = TypeVar("T")

PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
RETRIES = 2  # a GET that fails to connect, times out, or answers 429/502/503/504
RETRY_STATUS = frozenset({429, 502, 503, 504})
RETRY_AFTER_MAX = 30.0  # seconds: a longer Retry-After isn't waited out


class PluginIdError(ValueError):
    pass


def check_id(plugin_id: str) -> str:
    """A plugin id is permanent (its settings, data and URLs hang off it): 2 to 40
    lowercase letters, digits, - or _, starting with a letter."""
    if not PLUGIN_ID.match(plugin_id):
        raise PluginIdError(
            f"{plugin_id!r}: a plugin id is 2 to 40 lowercase letters, digits, - or _, "
            "starting with a letter"
        )
    return plugin_id


class _Pace:
    """At most ``per_second`` requests a second to one host, spaced evenly."""

    def __init__(self, per_second: float) -> None:
        self.gap = 1.0 / per_second
        self.next = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            if self.next > now:
                await asyncio.sleep(self.next - now)
                now = self.next
            self.next = now + self.gap


class PluginHttp:
    """``ctx.http``: an async HTTP client with Fetchbox's timeouts, retries for GETs and a
    User-Agent naming the plugin. Declare a host's rate limit once and every request to it
    keeps to it: ``ctx.http.limit("api.example.com", per_second=1)``."""

    def __init__(self, plugin_id: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.user_agent = f"Fetchbox/{__version__} (plugin {plugin_id})"
        self._client = httpx.AsyncClient(
            timeout=TIMEOUT,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
            transport=transport,
        )
        self._paces: dict[str, _Pace] = {}

    def limit(self, host: str, *, per_second: float) -> None:
        if per_second <= 0:
            raise ValueError("per_second must be more than 0")
        self._paces[host.lower()] = _Pace(per_second)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = httpx.URL(url).host.lower()
        retry = method.upper() in ("GET", "HEAD")
        for attempt in range(RETRIES + 1):
            if (pace := self._paces.get(host)) is not None:
                await pace.wait()
            last = attempt == RETRIES or not retry
            try:
                r = await self._client.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
                if last:
                    raise
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if r.status_code in RETRY_STATUS and not last:
                wait = _retry_after(r) or 0.5 * 2**attempt
                if wait <= RETRY_AFTER_MAX:
                    await r.aclose()
                    await asyncio.sleep(wait)
                    continue
            return r
        raise AssertionError("unreachable")  # pragma: no cover

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return max(float(r.headers.get("retry-after", "")), 0.0)
    except ValueError:
        return None


class PluginCache:
    """``ctx.cache``: answers kept for a while (API lookups), in memory, per plugin.
    ``await ctx.cache.remember(key, seconds, fetch)`` gives the kept answer or fetches it."""

    def __init__(self, max_entries: int = 1000) -> None:
        self.max_entries = max_entries
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str, default: Any = None) -> Any:
        hit = self._items.get(key)
        if hit is None or hit[0] < time.monotonic():
            self._items.pop(key, None)
            return default
        self._items.move_to_end(key)
        return hit[1]

    def set(self, key: str, value: Any, seconds: float) -> None:
        self._items[key] = (time.monotonic() + seconds, value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)  # the least recently used

    async def remember(self, key: str, seconds: float, fetch: Callable[[], Awaitable[T]]) -> T:
        missing = object()
        if (hit := self.get(key, missing)) is not missing:
            return hit  # type: ignore[no-any-return]
        value = await fetch()
        self.set(key, value, seconds)
        return value

    def clear(self) -> None:
        self._items.clear()


class PluginContext:
    """What a plugin gets: one per plugin, made when Fetchbox loads it."""

    def __init__(
        self,
        plugin_id: str,
        *,
        db: Database,
        data_dir: Path,
        services: Services | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.plugin_id = check_id(plugin_id)
        self._services = services
        self.log = structlog.get_logger("fetchbox.plugin").bind(plugin=plugin_id)
        self.http = PluginHttp(plugin_id, transport=transport)
        self.cache = PluginCache()
        self.store = PluginStore(db, plugin_id)
        self._folder = data_dir / "plugins" / plugin_id

    @property
    def folder(self) -> Path:
        """The plugin's own folder in the data folder (downloaded models, indexes, temp
        files). Made the first time it's asked for."""
        self._folder.mkdir(parents=True, exist_ok=True)
        return self._folder

    def _need(self) -> Services:
        if self._services is None:
            raise RuntimeError("this context has no Fetchbox services (made outside the app)")
        return self._services

    @property
    def files(self) -> PluginFiles:
        return PluginFiles(self.plugin_id, self._need())

    @property
    def library(self) -> PluginLibrary:
        return PluginLibrary(self.plugin_id, self._need())

    @property
    def jobs(self) -> PluginJobs:
        return PluginJobs(self.plugin_id, self._need().jobs)

    @property
    def notify(self) -> PluginNotify:
        return PluginNotify(self.plugin_id, self._need().notifications)

    async def aclose(self) -> None:
        await self.http.aclose()
