"""The plugin API package (fetchbox_plugin): what a plugin declares, and the checks it gets
the moment it declares something wrong."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import fetchbox_plugin
import pytest
from fetchbox_plugin import (
    PERMISSION_WORDS,
    Addon,
    Field,
    Folder,
    Panel,
    Permission,
    Plugin,
    PublicEvent,
    Secret,
    Settings,
    Url,
    action,
    every,
    on,
    panel,
)
from fetchbox_plugin.settings import has_test
from pydantic import ValidationError

import fetchbox.plugin


def test_fetchbox_plugin_is_the_same_api() -> None:
    for name in fetchbox_plugin.__all__:
        assert getattr(fetchbox.plugin, name) is getattr(fetchbox_plugin, name), name
    assert fetchbox_plugin.API_VERSION == 1


# ---- settings -----------------------------------------------------------------------------


class Example(Settings):
    key: Secret = Field(title="API key", help="Free at example.com")
    prefer: list[str] = Field(["cbz", "cbr"], title="Preferred formats, best first")
    folder: Folder = Field("", title="Comics folder")
    server: Url = Field("http://localhost:25600", title="Komga address")
    pages: int = Field(20, title="Per page", ge=1, le=100)


def test_settings_validate_and_hide_secrets() -> None:
    s = Example.model_validate({"key": "abc123"})
    assert s.key.value == "abc123" and s.key == Secret("abc123")
    assert "abc123" not in repr(s) and "abc123" not in str(s.key) and "abc123" not in f"{s.key}"
    assert s.model_dump(mode="json")["key"] == "abc123"  # stored (encrypted) by Fetchbox
    assert not Secret("") and Secret("x")
    with pytest.raises(ValidationError):
        Example.model_validate({"key": "k", "pages": 0})
    with pytest.raises(ValidationError):
        Example.model_validate({"key": "k", "colour": "red"})  # unknown fields are refused
    with pytest.raises(ValidationError):
        s.pages = 101  # validated on every change too


def test_settings_describe_their_form() -> None:
    props = Example.model_json_schema()["properties"]
    assert (
        props["key"]["title"] == "API key" and props["key"]["description"] == "Free at example.com"
    )
    assert props["key"]["format"] == "password" and props["key"]["writeOnly"] is True
    assert props["folder"]["x-fetchbox"] == "folder" and props["server"]["x-fetchbox"] == "url"
    assert props["pages"]["minimum"] == 1 and props["pages"]["maximum"] == 100
    assert props["prefer"]["default"] == ["cbz", "cbr"]


def test_a_test_button_is_optional() -> None:
    class WithTest(Settings):
        async def test(self, ctx: Any) -> str:
            return "works"

    assert has_test(WithTest) and not has_test(Example)


# ---- add-ons -------------------------------------------------------------------------------


class Subs(Addon):
    id = "subtitles"
    name = "Subtitles"
    applies_to = {"movie", "series"}  # noqa: RUF012 (how plugins declare it)
    settings = Example

    @on("file.imported", "file.upgraded")
    async def fetch(self, ctx: Any, event: PublicEvent) -> None: ...

    @action("Search subtitles", on="item")
    async def search_now(self, ctx: Any, item: Any) -> None: ...

    @every(hours=24)
    async def retry_missing(self, ctx: Any) -> None: ...

    @panel(on="item", title="Subtitles")
    async def subtitles_panel(self, ctx: Any, item: Any) -> Panel:
        return Panel(status="2 of 2 languages", actions=["Search subtitles"])


def test_an_addon_lists_what_it_does() -> None:
    h = Subs.handlers()
    assert [(n, s.events) for n, s in h["on"]] == [("fetch", ("file.imported", "file.upgraded"))]
    assert [(n, s.label, s.level) for n, s in h["action"]] == [
        ("search_now", "Search subtitles", "item")
    ]
    assert [(n, s.interval) for n, s in h["every"]] == [("retry_missing", timedelta(hours=24))]
    assert [(n, s.title) for n, s in h["panel"]] == [("subtitles_panel", "Subtitles")]
    assert Subs.settings_model is Example


@pytest.mark.parametrize(
    ("make", "why"),
    [
        (lambda: on("file.imported", "file.importd"), "not public events: file.importd"),
        (lambda: on(), "at least one event"),
        (lambda: action("Go", on="season"), "on= is one of"),  # type: ignore[arg-type]
        (lambda: action("  "), "needs a label"),
        (lambda: every(minutes=1), "at most every 5 minutes"),
        (lambda: every(), "at most every 5 minutes"),
        (lambda: panel(on="file", title="x"), "on= is one of"),  # type: ignore[arg-type]
    ],
)
def test_a_wrong_declaration_says_what_is_wrong(make: Any, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        make()


# ---- the manifest --------------------------------------------------------------------------


def test_a_plugin_declares_itself() -> None:
    class Subtitles(Plugin):
        id = "subtitles"
        name = "Subtitles"
        description = "Subtitles for movies and episodes."
        permissions = {Permission.NETWORK, Permission.LIBRARY_FILES}  # noqa: RUF012
        contributes = [Subs]  # noqa: RUF012

    assert Subtitles.fetchbox_api == 1 and Subtitles.contributes == [Subs]
    assert set(PERMISSION_WORDS) == set(Permission)  # each permission has its plain words
    assert PERMISSION_WORDS[Permission.NETWORK] == "Uses the network"


def test_an_event_knows_its_file() -> None:
    from datetime import UTC, datetime

    ev = PublicEvent(id="1", type="file.imported", occurred_at=datetime.now(UTC), file_ids=["f"])
    assert ev.file == "f"
    assert PublicEvent(id="2", type="system.started", occurred_at=ev.occurred_at).file is None
