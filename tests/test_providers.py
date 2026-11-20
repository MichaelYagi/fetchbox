from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import BaseModel

from fetchbox.core.providers import Provider, ProviderRegistry, TestResult


class _EchoSettings(BaseModel):
    url: str
    timeout: int = 10


class EchoIndexer(Provider):
    category = "indexer"
    kind = "echo"
    Settings: ClassVar[type[BaseModel]] = _EchoSettings
    capabilities = frozenset({"rss"})

    async def test(self) -> TestResult:
        return TestResult(ok=True, message=self.settings.url)  # type: ignore[attr-defined]


async def test_register_create_and_health() -> None:
    reg = ProviderRegistry()
    reg.register(EchoIndexer)
    p = reg.create("indexer", "echo", {"url": "http://x"})
    assert (await p.test()).message == "http://x"
    assert (await p.health()).status == "ok"
    assert reg.of("indexer") == [EchoIndexer]
    assert reg.settings_schema("indexer", "echo")["required"] == ["url"]


def test_duplicate_kind_rejected() -> None:
    reg = ProviderRegistry()
    reg.register(EchoIndexer)
    reg.register(EchoIndexer)  # idempotent for same class

    class Other(EchoIndexer):
        pass

    with pytest.raises(ValueError):
        reg.register(Other)


def test_unknown_kind() -> None:
    with pytest.raises(LookupError):
        ProviderRegistry().get("indexer", "nope")
