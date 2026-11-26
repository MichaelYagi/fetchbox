"""The media kind API: a kind declares itself, gets told at once what's wrong with the
declaration, and gets sensible defaults for the optional questions."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest

from fetchbox.core import kinds as core_kinds
from fetchbox_plugin import (
    File,
    Hit,
    Item,
    KindDeclarationError,
    MediaKind,
    Naming,
    Parsed,
    Qualities,
    Query,
    TitleDetails,
)
from fetchbox_plugin.kind import CORE_KINDS, KIND_ID

ISSUE = re.compile(r"^(?P<title>.+?) (?P<n>\d{3}) \((?P<year>\d{4})\)")


class Comics(MediaKind):
    id = "comics"
    name = "Comics"
    levels = ("series", "volume", "issue")
    file_types = frozenset({".cbz", ".cbr", ".pdf"})
    qualities = Qualities(["CBZ digital", "CBZ scan", "CBR", "PDF"])
    naming = Naming(
        "{series} ({start_year})/{series} #{issue:03} ({year})",
        tokens={"series", "start_year", "volume", "issue", "year", "title"},
    )

    async def search(self, ctx: Any, text: str) -> list[Hit]:
        return [Hit("4050-49901", "Saga", 2012)]

    async def title(self, ctx: Any, ref: str) -> TitleDetails:
        return TitleDetails(ref, "Saga", 2012, items=[Item("61", 61, released=date(2023, 1, 1))])

    def parse(self, release_name: str) -> Parsed | None:
        m = ISSUE.match(release_name)
        if not m:
            return None
        return Parsed(m["title"], (float(m["n"]),), year=int(m["year"]))

    def name_tokens(self, title: TitleDetails, item: Item) -> dict[str, object]:
        return {"series": title.name, "start_year": title.year, "issue": item.number,
                "year": item.released.year if item.released else None}  # fmt: skip


async def test_a_kind_answers_its_questions() -> None:
    k = Comics()
    assert (await k.search(None, "saga"))[0].ref == "4050-49901"
    t = await k.title(None, "4050-49901")
    assert k.parse("Saga 061 (2023) (Digital).cbz") == Parsed("Saga", (61.0,), year=2023)
    assert k.parse("Not a comic.mkv") is None
    tokens = k.name_tokens(t, t.items[0])
    assert k.naming.pattern.format(**tokens) == "Saga (2012)/Saga #061 (2023)"
    assert k.qualities.rank("CBZ digital") == 0 and k.qualities.rank("PDF") == 3


def test_default_queries_and_pick() -> None:
    k = Comics()
    t = TitleDetails("x", "Saga")
    items = [Item("61", 61), Item("62", 62), Item("half", 1.5), Item("special")]
    assert k.queries(t, items) == [Query("Saga 61"), Query("Saga 62"), Query("Saga 1.5"),
                                   Query("Saga")]  # fmt: skip

    one = [File("Saga 061/Saga 061.cbz", 40_000_000), File("Saga 061/cover.jpg", 90_000),
           File("Saga 061/Saga 061 (preview).cbz", 900_000)]  # fmt: skip
    assert k.pick(one, [Item("61", 61)]) == {"Saga 061/Saga 061.cbz": "61"}  # the largest

    pack = [File("Saga 061.cbz", 1), File("Saga 062.CBZ", 1), File("Saga 610.cbz", 1),
            File("notes.txt", 1)]  # fmt: skip
    assert k.pick(pack, [Item("61", 61), Item("62", 62), Item("63", 63)]) == {
        "Saga 061.cbz": "61",
        "Saga 062.CBZ": "62",
    }  # 610 isn't 61; 63 has no file
    assert k.pick([File("a.mkv", 9)], [Item("61", 61)]) == {}  # not the kind's file type


def _declare(**changes: Any) -> type[MediaKind]:
    body: dict[str, Any] = {
        "id": "comics",
        "name": "Comics",
        "levels": ("series", "issue"),
        "file_types": {".cbz"},
        "qualities": Qualities(["CBZ"]),
        "naming": Naming("{series} #{issue}", tokens={"series", "issue"}),
        **{m: getattr(Comics, m) for m in ("search", "title", "parse", "name_tokens")},
    }
    body.update(changes)
    for k in [k for k, v in body.items() if v is ...]:
        del body[k]
    return type("Declared", (MediaKind,), body)


def test_a_good_declaration_passes() -> None:
    assert _declare().id == "comics"
    assert _declare(id="audiobook", file_types={".m4b", ".mp3"}).id == "audiobook"  # mp3 is fine


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"id": "Comics"}, "lowercase"),
        ({"id": "graphicnovels"}, "2 to 10"),
        ({"id": "movie"}, "core kind"),
        ({"id": "series"}, "core kind"),
        ({"name": " "}, "name"),
        ({"levels": ()}, "levels"),
        ({"levels": ("a", "b", "c", "d")}, "levels"),
        ({"levels": ["series", "issue"]}, "levels"),
        ({"file_types": set()}, "file_types"),
        ({"file_types": {"cbz"}}, "not cbz"),
        ({"file_types": {".CBZ"}}, "not .CBZ"),
        ({"grab_level": "title"}, "grab_level"),
        ({"grab_level": "group"}, "three levels"),
        ({"qualities": ["CBZ"]}, "Qualities"),
        ({"naming": "{series}"}, "Naming"),
        ({"parse": ...}, r"parse\(\): required"),
        ({"search": ...}, r"search\(\): required"),
    ],
)
def test_a_wrong_declaration_says_what_to_change(change: dict[str, Any], why: str) -> None:
    with pytest.raises(KindDeclarationError, match=why):
        _declare(**change)


def test_every_problem_at_once() -> None:
    with pytest.raises(KindDeclarationError) as exc:
        _declare(id="X", levels=(), file_types=set())
    assert str(exc.value).count(";") >= 2  # one message listing each, not one at a time


@pytest.mark.parametrize(
    ("make", "why"),
    [
        (lambda: Qualities([]), "at least one"),
        (lambda: Qualities(["A", "A"]), "once"),
        (lambda: Naming("{series} #{issue}", tokens={"series"}), "issue: not in tokens"),
    ],
)
def test_qualities_and_naming_check_themselves(make: Any, why: str) -> None:
    with pytest.raises(KindDeclarationError, match=why):
        make()


def test_the_api_and_fetchbox_agree_on_kind_ids() -> None:
    """The API can't import Fetchbox, so it keeps its own copy of these rules."""
    assert KIND_ID.pattern == core_kinds._KEY.pattern
    assert set(core_kinds.CORE_KEYS) == CORE_KINDS
