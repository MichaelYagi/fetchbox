"""Discord: a channel webhook (Channel settings > Integrations > Webhooks > New Webhook)."""

from __future__ import annotations

import re
from typing import ClassVar

from pydantic import BaseModel, Field, SecretStr, field_validator

from fetchbox.core.providers import TestResult, registry
from fetchbox.core.secrets import strip_key
from fetchbox.providers.notifiers import Notification, NotifierError, NotifierProvider

_WEBHOOK = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[\w-]+$"
)
COLORS = {"info": 0x2563EB, "success": 0x16A34A, "warning": 0xD97706, "error": 0xDC2626}


class DiscordSettings(BaseModel):
    webhook_url: SecretStr = Field(
        description="The channel webhook's URL: Channel settings > Integrations > Webhooks > "
        "New Webhook > Copy Webhook URL"
    )

    @field_validator("webhook_url", mode="before")
    @classmethod
    def _check(cls, v: object) -> object:
        v = strip_key(v)
        raw = v.get_secret_value() if isinstance(v, SecretStr) else v
        if isinstance(raw, str) and not _WEBHOOK.match(raw):
            raise ValueError(
                "must be a Discord webhook URL, https://discord.com/api/webhooks/<id>/<token>"
            )
        return v


def embed(n: Notification) -> dict[str, object]:
    """Discord's webhook body: one embed within Discord's limits."""
    body = n.body
    if n.links:
        body += ("\n\n" if body else "") + " · ".join(f"[{x.label}]({x.url})" for x in n.links)
    e: dict[str, object] = {
        "title": n.title[:256],
        "description": body[:4096],
        "color": COLORS[n.level],
        "timestamp": n.occurred_at.isoformat(),
        "footer": {"text": f"Fetchbox · {n.event}"[:2048]},
    }
    if n.image_url:
        e["thumbnail"] = {"url": n.image_url}
    return {"username": "Fetchbox", "embeds": [e], "allowed_mentions": {"parse": []}}


class DiscordNotifier(NotifierProvider):
    kind: ClassVar[str] = "discord"
    Settings: ClassVar[type[BaseModel]] = DiscordSettings

    async def send(self, n: Notification) -> None:
        cfg = self.settings
        assert isinstance(cfg, DiscordSettings)
        await self._post(cfg.webhook_url.get_secret_value(), embed(n))

    async def test(self) -> TestResult:
        from fetchbox.core.db import utcnow

        try:
            await self.send(Notification(event="test", title="Fetchbox test",
                                         body="Notifications work.", level="success",
                                         occurred_at=utcnow()))  # fmt: skip
        except NotifierError as exc:
            return TestResult(ok=False, message=f"Discord: {exc.message}")
        return TestResult(ok=True, message="Sent a test message to the Discord channel")


registry.register(DiscordNotifier)
