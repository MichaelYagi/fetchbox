"""Webhook: POST the notification as JSON to any URL (Home Assistant, n8n, ...)."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field, SecretStr, field_validator

from fetchbox.core.providers import TestResult, registry
from fetchbox.providers.media_servers import check_url
from fetchbox.providers.notifiers import Notification, NotifierError, NotifierProvider


class WebhookSettings(BaseModel):
    url: str = Field(
        description="Where to POST, e.g. http://homeassistant.local:8123/api/webhook/fetchbox"
    )
    secret: SecretStr | None = Field(
        default=None, description="Optional: sent as the X-Fetchbox-Secret header"
    )

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return check_url(v)


class WebhookNotifier(NotifierProvider):
    kind: ClassVar[str] = "webhook"
    Settings: ClassVar[type[BaseModel]] = WebhookSettings

    async def send(self, n: Notification) -> None:
        cfg = self.settings
        assert isinstance(cfg, WebhookSettings)
        headers = {"X-Fetchbox-Event": n.event}
        if cfg.secret:
            headers["X-Fetchbox-Secret"] = cfg.secret.get_secret_value()
        await self._post(cfg.url, n.model_dump(mode="json"), headers)

    async def test(self) -> TestResult:
        from fetchbox.core.db import utcnow

        try:
            test = Notification(
                event="test",
                title="Fetchbox test",
                body="Notifications work.",
                occurred_at=utcnow(),
            )
            await self.send(test)
        except NotifierError as exc:
            return TestResult(ok=False, message=exc.message)
        return TestResult(ok=True, message="Sent a test notification")


registry.register(WebhookNotifier)
