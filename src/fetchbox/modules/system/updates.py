"""Update check: is there a newer Fetchbox release? (spec: "Migrations and updates")

Asks GitHub for the public repo's latest release, at most every 6 hours, and only when
``updates.check`` is on (default; it contacts api.github.com, so it can be turned off).
Nothing is downloaded or installed: the UI shows "update available" with a link.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

import fetchbox

REPO = "MichaelYagi/fetchbox"
FEED = f"https://api.github.com/repos/{REPO}/releases/latest"
TTL = 6 * 3600.0


def _parts(v: str) -> tuple[int, ...] | None:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", v.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(latest: str, current: str) -> bool:
    """True when ``latest`` is a later release; a dev build (0.1.0.dev0) counts as the
    release it leads up to, so 0.1.0 isn't offered to it, 0.1.1 is."""
    a, b = _parts(latest), _parts(current)
    return a is not None and b is not None and a > b


@dataclass
class UpdateInfo:
    current: str
    latest: str | None
    update_available: bool
    url: str | None
    published_at: str | None
    checked: bool
    error: str | None = None
    downloads: list[dict[str, Any]] = field(default_factory=list)  # the release's files


class UpdateChecker:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport
        self._cached: tuple[float, UpdateInfo] | None = None

    async def check(self, *, enabled: bool, fresh: bool = False) -> UpdateInfo:
        current = fetchbox.__version__
        if not enabled:
            return UpdateInfo(current, None, False, None, None, checked=False)
        if not fresh and self._cached and time.monotonic() - self._cached[0] < TTL:
            return self._cached[1]
        try:
            async with httpx.AsyncClient(timeout=10.0, transport=self.transport) as http:
                r = await http.get(FEED, headers={"Accept": "application/vnd.github+json",
                                                  "User-Agent": "Fetchbox"})  # fmt: skip
            if r.status_code == 404:  # no release published yet
                info = UpdateInfo(current, None, False, None, None, checked=True)
            else:
                r.raise_for_status()
                d = r.json()
                tag = str(d.get("tag_name") or "")
                files = [
                    {"name": a["name"], "url": a["browser_download_url"], "size": a.get("size")}
                    for a in d.get("assets") or []
                    if a.get("name") and a.get("browser_download_url")
                ]
                info = UpdateInfo(
                    current, tag or None, newer(tag, current), d.get("html_url"),
                    d.get("published_at"), checked=True, downloads=files,
                )  # fmt: skip
        except (httpx.HTTPError, ValueError) as exc:
            return UpdateInfo(current, None, False, None, None, checked=False,
                              error=f"couldn't check: {type(exc).__name__}")  # fmt: skip
        self._cached = (time.monotonic(), info)
        return info
