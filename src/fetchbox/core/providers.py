"""Provider protocol and registry.

Every external dependency (metadata, indexers, download clients, storage, media
servers, notifiers) is a provider: a class registered under a ``kind`` string with a
Pydantic ``Settings`` model that drives validation and the UI form. Built-ins register
in-tree; third-party providers load from the ``fetchbox.providers`` entry-point group.
"""

from __future__ import annotations

from collections.abc import Iterator
from importlib.metadata import entry_points
from typing import Any, ClassVar, Literal

import structlog
from pydantic import BaseModel, ConfigDict

log = structlog.get_logger(__name__)

ENTRY_POINT_GROUP = "fetchbox.providers"

ProviderCategory = Literal[
    "metadata", "indexer", "download_client", "storage", "media_server", "notifier"
]


class TestResult(BaseModel):
    __test__ = False  # not a pytest test class

    ok: bool
    message: str = ""
    details: dict[str, Any] = {}


HealthLevel = Literal["ok", "warning", "error"]


class HealthStatus(BaseModel):
    status: HealthLevel
    message: str = ""


class Provider:
    """Base class for all providers. Subclasses set the ClassVars and implement test/health."""

    category: ClassVar[ProviderCategory]
    kind: ClassVar[str]
    Settings: ClassVar[type[BaseModel]]
    capabilities: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, settings: BaseModel) -> None:
        self.settings = settings

    async def test(self) -> TestResult:
        raise NotImplementedError

    async def health(self) -> HealthStatus:
        res = await self.test()
        return HealthStatus(status="ok" if res.ok else "error", message=res.message)


class ProviderRegistry:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], type[Provider]] = {}

    def register(self, cls: type[Provider]) -> type[Provider]:
        for attr in ("category", "kind", "Settings"):
            if not hasattr(cls, attr):
                raise TypeError(f"{cls.__qualname__} is missing ClassVar {attr!r}")
        key = (cls.category, cls.kind)
        existing = self._by_key.get(key)
        if existing is not None and existing is not cls:
            raise ValueError(f"provider {key} already registered by {existing.__qualname__}")
        self._by_key[key] = cls
        return cls

    def unregister(self, cls: type[Provider]) -> None:
        """A provider gone (its plugin turned off). What was set up with it stays, shown as
        it was, and says plainly that it can't be used until the plugin is back."""
        key = (cls.category, cls.kind)
        if self._by_key.get(key) is cls:
            self._by_key[key] = _unavailable(cls)

    def get(self, category: ProviderCategory, kind: str) -> type[Provider]:
        try:
            return self._by_key[(category, kind)]
        except KeyError:
            raise LookupError(f"no {category} provider of kind {kind!r}") from None

    def of(self, category: ProviderCategory) -> list[type[Provider]]:
        return [
            c for (cat, _), c in sorted(self._by_key.items())
            if cat == category and not getattr(c, "unavailable", False)
        ]  # fmt: skip

    def __iter__(self) -> Iterator[type[Provider]]:
        return iter(self._by_key.values())

    def create(self, category: ProviderCategory, kind: str, settings: dict[str, Any]) -> Provider:
        cls = self.get(category, kind)
        return cls(cls.Settings.model_validate(settings))

    def settings_schema(self, category: ProviderCategory, kind: str) -> dict[str, Any]:
        """JSON Schema that the UI renders as the provider's settings form."""
        return self.get(category, kind).Settings.model_json_schema()

    def load_entry_points(self) -> list[str]:
        loaded: list[str] = []
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                obj = ep.load()
                classes = obj if isinstance(obj, list | tuple) else [obj]
                for cls in classes:
                    self.register(cls)
                loaded.append(ep.name)
            except Exception:
                log.exception("provider_plugin_failed", entry_point=ep.name)
        return loaded


registry = ProviderRegistry()


class ProviderUnavailable(RuntimeError):
    pass


def _unavailable(gone: type[Provider]) -> type[Provider]:
    """A stand-in for a plugin's provider while the plugin is off: its settings (any shape)
    still read, and every use says why it can't work."""
    why = f"{gone.kind}: the plugin that provides it is off"

    class Settings(BaseModel):
        model_config = ConfigDict(extra="allow")

    class Unavailable(Provider):
        category = gone.category
        kind = gone.kind
        unavailable = True

        async def test(self) -> TestResult:
            return TestResult(ok=False, message=why)

        async def health(self) -> HealthStatus:
            return HealthStatus(status="warning", message=why)

        def __getattr__(self, name: str) -> Any:
            async def fails(*_a: Any, **_k: Any) -> Any:
                raise ProviderUnavailable(why)

            return fails

    Unavailable.Settings = Settings
    Unavailable.__qualname__ = f"Unavailable[{gone.__qualname__}]"
    return Unavailable
