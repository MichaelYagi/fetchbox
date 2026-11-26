"""A plugin's settings: one model, which Fetchbox turns into a settings tab with validation,
help text and a Test button. Plugins write no form code.

    class ComicsSettings(Settings):
        comicvine_key: Secret = Field(title="ComicVine API key", help="Free at comicvine...")
        prefer: list[str] = Field(["cbz", "cbr"], title="Preferred formats, best first")
        folder: Folder = Field(title="Comics folder")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

import pydantic
from pydantic import BaseModel, ConfigDict, GetCoreSchemaHandler
from pydantic_core import core_schema

if TYPE_CHECKING:
    from fetchbox_plugin.context import Context

MASK = "••••••••"


class Secret:
    """A password or API key: a password field, never sent back to the browser, encrypted
    in Fetchbox's database. Read it with ``.value``."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return f"Secret({MASK if self._value else ''!r})"

    __str__ = __repr__  # never the value by accident (in a log line, an f-string)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> Any:
        from_str = core_schema.no_info_plain_validator_function(
            lambda v: v if isinstance(v, Secret) else Secret(str(v))
        )
        return core_schema.json_or_python_schema(
            json_schema=core_schema.chain_schema([core_schema.str_schema(), from_str]),
            python_schema=core_schema.union_schema(
                [core_schema.is_instance_schema(Secret), core_schema.chain_schema(
                    [core_schema.str_schema(), from_str])]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda v: v.value, when_used="always"
            ),
            metadata={"pydantic_js_updates": {"format": "password", "writeOnly": True}},
        )  # fmt: skip


class _Folder:
    """Marks a ``str`` as a library folder: shown as Fetchbox's folder picker, with the same
    checks as the core types' folders (online, writable, not another library folder)."""


class _Url:
    """Marks a ``str`` as a web address: shown with a Test of whether it's reachable."""


Folder = Annotated[str, _Folder(), pydantic.Field(json_schema_extra={"x-fetchbox": "folder"})]
Url = Annotated[str, _Url(), pydantic.Field(json_schema_extra={"x-fetchbox": "url"})]


def Field(
    default: Any = ...,
    *,
    title: str | None = None,
    help: str | None = None,
    ge: float | None = None,
    le: float | None = None,
    **kwargs: Any,
) -> Any:
    """A setting: ``title`` is its label, ``help`` the hint under it, ``ge``/``le`` limits."""
    return pydantic.Field(default, title=title, description=help, ge=ge, le=le, **kwargs)


class Settings(BaseModel):
    """Base for a plugin's settings. Validated on every save; unknown fields are refused."""

    # defaults are checked too: a Secret with a default is a Secret, not a str
    model_config = ConfigDict(extra="forbid", validate_assignment=True, validate_default=True)

    async def test(self, ctx: Context) -> str:
        """Optional: the Test button. Return what worked; raise to show what didn't."""
        raise NotImplementedError


def has_test(settings: type[Settings]) -> bool:
    return settings.test is not Settings.test
