"""Runtime configuration edited through the API (``GET/PATCH /system/config``).

Stored as one versioned JSON row; secret fields are encrypted with the install key.
Environment variables (``FETCHBOX_TMDB_API_KEY``) override the stored value, matching
the precedence in ``fetchbox.config``; the API reports which fields are overridden.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator

from fetchbox.core.concurrency import PreconditionFailed, check_if_match
from fetchbox.core.db import Database
from fetchbox.core.errors import FieldError, ProblemError
from fetchbox.core.events import Actor, EventBus
from fetchbox.core.secrets import MASK, SecretBox, is_encrypted, strip_key
from fetchbox.modules.system.models import ConfigRow

ROW_ID = "system"
_AAD = "config"


class TmdbConfig(BaseModel):
    @field_validator("api_key", mode="before")
    @classmethod
    def _strip_key(cls, v: object) -> object:
        return strip_key(v)  # a pasted key often carries a trailing space or newline

    api_key: SecretStr | None = Field(
        default=None, description="TMDB API key (v3) or read access token (v4)"
    )
    language: str = Field(default="en-US", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    include_adult: bool = False


class TvdbConfig(BaseModel):
    """Optional: only for absolute episode numbers (anime)."""

    @field_validator("api_key", "pin", mode="before")
    @classmethod
    def _strip_key(cls, v: object) -> object:
        return strip_key(v)

    api_key: SecretStr | None = Field(default=None, description="TheTVDB v4 API key")
    pin: SecretStr | None = Field(default=None, description="Subscriber PIN, for user keys")


class SearchConfig(BaseModel):
    min_seeders: int = Field(default=1, ge=0, description="Reject torrents with fewer seeders")
    season_pack_share: float = Field(
        default=1.0,
        gt=0,
        le=1,
        description="Share of a finished season's episodes that must be wanted to grab a pack",
    )
    automatic: bool = Field(
        default=True, description="Search for missing items and upgrades on a schedule"
    )
    missing_every_hours: int = Field(
        default=6, ge=1, le=24 * 30, description="How often each missing item is searched"
    )
    upgrade_every_hours: int = Field(
        default=24, ge=1, le=24 * 30, description="How often each upgradable item is searched"
    )
    retry_alternatives: int = Field(
        default=3,
        ge=0,
        le=20,
        description="After a failed download, search again at once this many times",
    )


class StorageConfig(BaseModel):
    recycle_days: int = Field(
        default=7, ge=1, le=365, description="Days deleted files stay in recycle folders"
    )


class UpdatesConfig(BaseModel):
    check: bool = Field(
        default=True,
        description="Check GitHub for a newer Fetchbox release (every 6 hours at most)",
    )


class SystemConfig(BaseModel):
    tmdb: TmdbConfig = TmdbConfig()
    tvdb: TvdbConfig = TvdbConfig()
    search: SearchConfig = SearchConfig()
    storage: StorageConfig = StorageConfig()
    updates: UpdatesConfig = UpdatesConfig()


SECRETS: tuple[tuple[str, str], ...] = (("tmdb", "api_key"), ("tvdb", "api_key"), ("tvdb", "pin"))


Listener = Callable[[SystemConfig], Awaitable[None]]


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        out[k] = (
            _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        )
    return out


def _changed_paths(a: dict[str, Any], b: dict[str, Any], prefix: str = "") -> list[str]:
    paths: list[str] = []
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k), b.get(k)
        if isinstance(va, dict) and isinstance(vb, dict):
            paths += _changed_paths(va, vb, f"{prefix}{k}.")
        elif va != vb:
            paths.append(prefix + k)
    return paths


def _reveal(cfg: SystemConfig) -> dict[str, Any]:
    """Dump with secret values in clear, for change detection only."""
    data = cfg.model_dump(mode="python")
    for section, name in SECRETS:
        v = data[section].get(name)
        data[section][name] = v.get_secret_value() if isinstance(v, SecretStr) else v
    return data


class ConfigService:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        box: SecretBox,
        *,
        env: dict[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.bus = bus
        self.box = box
        self.env = env or {}  # dotted path -> value, e.g. {"tmdb.api_key": "..."}
        self._listeners: list[Listener] = []
        self._current = SystemConfig()
        self._stored: dict[str, Any] = {}  # sealed form, as in the DB
        self.version = 0

    def on_change(self, listener: Listener) -> None:
        self._listeners.append(listener)

    @property
    def current(self) -> SystemConfig:
        """Effective config: stored values with environment overrides applied."""
        return self._current

    async def load(self) -> None:
        async with self.db.sessionmaker() as s:
            row = await s.get(ConfigRow, ROW_ID)
        self._stored = dict(row.data) if row else {}
        self.version = row.version if row else 0
        self._current = self._effective(self._stored)

    def _open(self, stored: dict[str, Any]) -> dict[str, Any]:
        data = _deep_merge({}, stored)
        for section, name in SECRETS:
            v = data.get(section, {}).get(name)
            if is_encrypted(v):
                data[section][name] = self.box.decrypt(v, aad=f"{_AAD}:{section}.{name}")
        return data

    def _seal(self, cfg: SystemConfig) -> dict[str, Any]:
        data = cfg.model_dump(mode="python")
        for section, name in SECRETS:
            v = data[section].get(name)
            if isinstance(v, SecretStr):
                v = v.get_secret_value()
            data[section][name] = self.box.encrypt(v, aad=f"{_AAD}:{section}.{name}") if v else None
        return data

    def _effective(self, stored: dict[str, Any]) -> SystemConfig:
        data = self._open(stored)
        for path, value in self.env.items():
            section, name = path.split(".", 1)
            data.setdefault(section, {})[name] = value
        return SystemConfig.model_validate(data)

    def view(self) -> dict[str, Any]:
        """API view: secrets masked, plus which fields the environment overrides."""
        data = self._current.model_dump(mode="json")
        for section, name in SECRETS:
            data[section][name] = MASK if getattr(getattr(self._current, section), name) else None
        return {"config": data, "overridden": sorted(self.env), "version": self.version}

    async def update(
        self, patch: dict[str, Any], *, if_match: str | None, actor: Actor
    ) -> dict[str, Any]:
        bad = [
            FieldError(field=f"body.{k}", message="must be an object of settings")
            if k in SystemConfig.model_fields
            else FieldError(field=f"body.{k}", message="unknown config section")
            for k, v in patch.items()
            if k not in SystemConfig.model_fields or not isinstance(v, dict)
        ]
        for k, v in patch.items():
            if isinstance(v, dict) and k in SystemConfig.model_fields:
                section = getattr(SystemConfig(), k)
                bad += [
                    FieldError(field=f"body.{k}.{name}", message="unknown setting")
                    for name in v
                    if name not in type(section).model_fields
                ]
        if bad:
            raise ProblemError(422, "Validation Failed", slug="validation", errors=bad)
        for path in self.env:
            section, name = path.split(".", 1)
            if name in patch.get(section, {}):
                raise ProblemError(
                    409,
                    "Conflict",
                    f"{path} is set by an environment variable; change it there",
                    slug="conflict",
                )
        async with self.db.sessionmaker() as s, s.begin():
            row = await s.get(ConfigRow, ROW_ID)
            if if_match is not None:
                if row is None:
                    if if_match.strip() != '"v0"':  # nothing stored yet is version 0
                        raise PreconditionFailed()
                else:
                    check_if_match(row, if_match)
            stored = dict(row.data) if row else {}
            plain = self._open(stored)
            for section, name in SECRETS:  # MASK sent back = keep the current secret
                if patch.get(section, {}).get(name) == MASK:
                    patch[section][name] = plain.get(section, {}).get(name)
            try:
                cfg = SystemConfig.model_validate(_deep_merge(plain, patch))
            except ValidationError as exc:
                raise ProblemError(
                    422,
                    "Validation Failed",
                    slug="validation",
                    errors=[
                        FieldError(
                            field=".".join(str(p) for p in ("body", *e["loc"])),
                            message=e["msg"].removeprefix("Value error, "),
                        )
                        for e in exc.errors()
                    ],
                ) from None
            before = _reveal(SystemConfig.model_validate(plain))
            changed = _changed_paths(before, _reveal(cfg))
            sealed = self._seal(cfg)
            if row is None:
                row = ConfigRow(id=ROW_ID, data=sealed)
                s.add(row)
            elif changed:
                row.data = sealed
            await s.flush()
            version = row.version
        self._stored, self.version = sealed, version
        self._current = self._effective(sealed)
        if changed:
            # Values are never put on the bus: they may be secrets.
            await self.bus.emit("system.config_changed", actor=actor, fields=changed)
            for listener in self._listeners:
                await listener(self._current)
        return self.view()
