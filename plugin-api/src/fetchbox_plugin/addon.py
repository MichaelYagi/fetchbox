"""Add-ons: something added to titles Fetchbox already has (subtitles, artwork).

    class Subtitles(Addon):
        id = "subtitles"
        name = "Subtitles"
        applies_to = {"movie", "series"}
        settings = SubtitleSettings

        @on("file.imported")
        async def fetch(self, ctx, event): ...

        @action("Search subtitles", on="item")
        async def search_now(self, ctx, item): ...

        @every(hours=24)
        async def retry_missing(self, ctx): ...

Handlers run as background jobs after the event's own work is done, so a slow or failing
add-on never delays an import.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar, Literal, TypeVar

from fetchbox_plugin.events import PUBLIC_EVENTS
from fetchbox_plugin.settings import Settings

F = TypeVar("F", bound=Callable[..., Any])
# a movie or show; a season or album; an episode...; the whole library (on the plugin's card)
Level = Literal["title", "group", "item", "library"]
LEVELS: tuple[Level, ...] = ("title", "group", "item", "library")
MIN_EVERY = timedelta(minutes=5)


@dataclass(frozen=True)
class OnSpec:
    events: tuple[str, ...]


@dataclass(frozen=True)
class ActionSpec:
    label: str
    level: Level


@dataclass(frozen=True)
class EverySpec:
    interval: timedelta


@dataclass(frozen=True)
class PanelSpec:
    level: Level
    title: str


def on(*events: str) -> Callable[[F], F]:
    """Run the method for each of these public events: ``async def m(self, ctx, event)``."""
    if not events:
        raise ValueError("@on needs at least one event")
    unknown = [e for e in events if e not in PUBLIC_EVENTS]
    if unknown:
        raise ValueError(f"not public events: {', '.join(unknown)} (see PUBLIC_EVENTS)")

    def deco(fn: F) -> F:
        fn.__fetchbox_on__ = OnSpec(tuple(events))  # type: ignore[attr-defined]
        return fn

    return deco


def action(label: str, *, on: Level = "item") -> Callable[[F], F]:
    """A button on each title, group or item: ``async def m(self, ctx, item)``. `item` is the
    title's id, the item's id, or for a group "<title id>:<group>" (a show's season: its
    number, "…:2"; shown on season rows from Fetchbox 0.10.4). Or, with
    ``on="library"``, one button for the whole library on the plugin's card in Settings >
    Plugins: ``async def m(self, ctx)`` (Fetchbox 0.10.4 or later). Runs as a job and shows
    its progress and result."""
    if on not in LEVELS:
        raise ValueError(f"on= is one of {', '.join(LEVELS)}")
    if not label.strip():
        raise ValueError("an action needs a label")

    def deco(fn: F) -> F:
        fn.__fetchbox_action__ = ActionSpec(label, on)  # type: ignore[attr-defined]
        return fn

    return deco


def every(*, days: float = 0, hours: float = 0, minutes: float = 0) -> Callable[[F], F]:
    """A scheduled job, shown in Fetchbox's job list: ``async def m(self, ctx)``."""
    interval = timedelta(days=days, hours=hours, minutes=minutes)
    if interval < MIN_EVERY:
        raise ValueError(
            f"@every runs at most every {int(MIN_EVERY.total_seconds() // 60)} minutes"
        )

    def deco(fn: F) -> F:
        fn.__fetchbox_every__ = EverySpec(interval)  # type: ignore[attr-defined]
        return fn

    return deco


def panel(*, on: Level, title: str) -> Callable[[F], F]:
    """A panel on the title page: ``async def m(self, ctx, item) -> Panel``. Fetchbox draws
    it in its own style, so plugins ship no JavaScript."""
    if on not in LEVELS:
        raise ValueError(f"on= is one of {', '.join(LEVELS)}")

    def deco(fn: F) -> F:
        fn.__fetchbox_panel__ = PanelSpec(on, title)  # type: ignore[attr-defined]
        return fn

    return deco


@dataclass
class Panel:
    """What a panel shows: a status line, a table, and buttons for the add-on's actions."""

    status: str = ""
    table: list[dict[str, Any]] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)  # labels of @action methods


class Addon:
    """Something that reacts to Fetchbox: methods decorated with ``@on`` (events),
    ``@action`` (buttons), ``@every`` (schedules) and ``@panel`` (title page panels). List it
    in your Plugin's ``contributes``; ``settings = MySettings`` gives it settings."""

    id: ClassVar[str]
    name: ClassVar[str]
    applies_to: ClassVar[frozenset[str] | set[str] | None] = None  # media kinds; None: all
    settings_model: ClassVar[type[Settings] | None] = None

    settings: Any = None  # the validated settings, set by Fetchbox before any method runs

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        model = cls.__dict__.get("settings")
        if isinstance(model, type) and issubclass(model, Settings):
            cls.settings_model = model  # `settings = MySettings` declares the model

    @classmethod
    def handlers(cls) -> dict[str, list[tuple[str, Any]]]:
        """The add-on's decorated methods by what they are: on, action, every, panel."""
        out: dict[str, list[tuple[str, Any]]] = {"on": [], "action": [], "every": [], "panel": []}
        for name in dir(cls):
            fn = getattr(cls, name, None)
            for what in out:
                if (spec := getattr(fn, f"__fetchbox_{what}__", None)) is not None:
                    out[what].append((name, spec))
        return out
