"""Notifiers: send a short message somewhere when something happens."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, Field

from fetchbox.core.netguard import BlockedAddress, public_target
from fetchbox.core.providers import Provider, ProviderCategory

Level = Literal["info", "success", "warning", "error"]
TIMEOUT = httpx.Timeout(15.0, connect=10.0)


class Link(BaseModel):
    label: str
    url: str


class Notification(BaseModel):
    event: str  # e.g. import.completed
    title: str
    body: str = ""
    level: Level = "info"
    image_url: str | None = None
    links: list[Link] = []
    occurred_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class NotifierError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotifierProvider(Provider):
    category: ClassVar[ProviderCategory] = "notifier"

    def __init__(
        self,
        settings: BaseModel,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        public_only: bool = False,
    ) -> None:
        super().__init__(settings)
        self._transport = transport
        self._public_only = public_only  # Settings > Network: no private addresses

    async def send(self, n: Notification) -> None:
        raise NotImplementedError

    async def _post(self, url: str, json: Any, headers: dict[str, str] | None = None) -> None:
        target: httpx.URL | str = url
        extra: dict[str, str] = {}
        ext: dict[str, object] = {}
        if self._public_only:
            try:
                target, extra, ext = await public_target(url)
            except BlockedAddress as exc:
                raise NotifierError(exc.message) from None
        # Redirects aren't followed: a public address can't hand over to a private one.
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as http:
            try:
                r = await http.post(
                    target,
                    json=json,
                    headers={"User-Agent": "Fetchbox", **(headers or {}), **extra},
                    extensions=ext,
                )
            except httpx.HTTPError as exc:
                raise NotifierError(
                    f"couldn't reach {httpx.URL(url).host}: {type(exc).__name__}"
                ) from None
        if r.status_code >= 400:
            detail = r.text[:200].strip()
            raise NotifierError(f"HTTP {r.status_code}" + (f": {detail}" if detail else ""))
