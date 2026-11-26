"""Media kinds: a new kind of thing to collect (comics, ebooks, audiobooks).

A kind describes its things as a tree of up to three levels and answers six questions about
them; Fetchbox stores the tree and runs the whole pipeline (search, grab, download, import,
naming, repair, history, the Add page, a Library tab, title pages).

    class ComicsKind(MediaKind):
        id = "comics"
        name = "Comics"
        levels = ("series", "volume", "issue")
        file_types = {".cbz", ".cbr", ".cb7", ".pdf"}
        qualities = Qualities(["CBZ digital", "CBZ scan", "CBR", "PDF"])
        naming = Naming("{series} ({start_year})/{series} #{issue:03} ({year})",
                        tokens={"series", "start_year", "volume", "issue", "year", "title"})

        async def search(self, ctx, text): ...        # 1 the Add page's search
        async def title(self, ctx, ref): ...          # 2 the tree and release dates
        def queries(self, title, items): ...          # 3 what to ask indexers (optional)
        def parse(self, release_name): ...            # 4 read a release name
        def pick(self, files, items): ...             # 5 which file is which item (optional)
        def name_tokens(self, title, item): ...       # 6 values for the naming

Movies (movie), TV (show, season, episode) and music (artist, album, track) fit the same
tree; they stay built into Fetchbox.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from fetchbox_plugin.settings import Settings

if TYPE_CHECKING:
    from fetchbox_plugin.context import Context

# A kind's id is stored with every title and library folder: 2 to 10 characters
# (Fetchbox's kind columns), lowercase. Fetchbox's own check is the same (a test keeps it so).
KIND_ID = re.compile(r"^[a-z][a-z0-9_]{1,9}$")
CORE_KINDS = frozenset({"movie", "series", "music"})  # built in; never a plugin's


class KindDeclarationError(TypeError):
    """A media kind declared wrong; the message says what to change."""


# ---- what a kind answers with ---------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    """One search result on the Add page."""

    ref: str  # your metadata source's id for it ("4050-49901"); given back to title()
    name: str
    year: int | None = None
    overview: str = ""
    poster_url: str | None = None


@dataclass(frozen=True)
class Item:
    """What's wanted and downloaded: an issue, a book, an episode."""

    ref: str  # stable within its title
    number: float | None = None  # 61, 1.5; None when items aren't numbered
    name: str | None = None
    released: date | None = None  # None: not announced; in the future: not searched yet
    group: str | None = None  # the ref of the Group it's in


@dataclass(frozen=True)
class Group:
    """The middle level (a volume, a series of books); kinds with two levels have none."""

    ref: str
    number: float | None = None
    name: str | None = None


@dataclass(frozen=True)
class TitleDetails:
    """A title as the kind's metadata source describes it: its groups and items with their
    numbers, names and release dates. Asked for when it's added and again on refresh."""

    ref: str
    name: str
    year: int | None = None
    overview: str = ""
    poster_url: str | None = None
    groups: list[Group] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # yours: given back to name_tokens


@dataclass(frozen=True)
class Query:
    """One search to send to indexers."""

    text: str
    categories: tuple[int, ...] = ()  # Newznab/Torznab categories; () = the kind's default


@dataclass(frozen=True)
class Parsed:
    """What a release name says (parse() returns None for one that isn't this kind's)."""

    title: str  # the title words, as written: "Saga"
    numbers: tuple[float, ...] = ()  # the items it holds: (61,), or (1, 2, 3) for a pack
    group: float | None = None  # a volume's number, when it's a whole volume
    year: int | None = None
    quality: str | None = None  # one of the kind's qualities, when the name says
    release_group: str | None = None


@dataclass(frozen=True)
class File:
    """A file in a finished download, as pick() sees it."""

    path: str  # inside the download, with / between folders
    size: int


# ---- what a kind declares -------------------------------------------------------------------


class Qualities:
    """The kind's qualities, best first. Fetchbox's quality profiles for the kind are made
    from them, and an upgrade is a file of a better one."""

    def __init__(self, names: list[str]) -> None:
        if not names:
            raise KindDeclarationError("Qualities needs at least one quality")
        if len(set(names)) != len(names):
            raise KindDeclarationError("each quality is listed once")
        self.names = tuple(names)

    def rank(self, name: str) -> int:
        """0 for the best; ValueError for a quality that isn't the kind's."""
        return self.names.index(name)


class Naming:
    """The default naming for the kind's files, with the tokens it may use. People change
    the pattern in Settings; the tokens are what name_tokens() fills in."""

    def __init__(self, pattern: str, *, tokens: set[str] | frozenset[str]) -> None:
        used = {f for _, f, _, _ in string.Formatter().parse(pattern) if f}
        unknown = sorted({u.split(".")[0].split("[")[0] for u in used} - set(tokens))
        if unknown:
            raise KindDeclarationError(f"the naming uses {', '.join(unknown)}: not in tokens")
        self.pattern = pattern
        self.tokens = frozenset(tokens)


class MediaKind:
    id: ClassVar[str]
    name: ClassVar[str]  # the Add page button and the Library tab: "Comics"
    levels: ClassVar[tuple[str, ...]]  # the words the UI uses, title first: ("series", "issue")
    file_types: ClassVar[set[str] | frozenset[str]]  # with the dot: {".cbz", ".cbr"}
    grab_level: ClassVar[Literal["item", "group"]] = "item"  # music grabs whole albums
    qualities: ClassVar[Qualities]
    naming: ClassVar[Naming]
    settings_model: ClassVar[type[Settings] | None] = None
    categories: ClassVar[tuple[int, ...]] = ()  # indexer categories searched by default

    settings: Any = None  # the validated settings, set by Fetchbox before any method runs

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        model = cls.__dict__.get("settings")
        if isinstance(model, type) and issubclass(model, Settings):
            cls.settings_model = model  # `settings = MySettings` declares the model
        if "id" in cls.__dict__:
            check_kind(cls)

    async def search(self, ctx: Context, text: str) -> list[Hit]:
        """1. Titles matching what someone typed on the Add page."""
        raise NotImplementedError

    async def title(self, ctx: Context, ref: str) -> TitleDetails:
        """2. A title's groups and items, with numbers, names and release dates."""
        raise NotImplementedError

    def queries(self, title: TitleDetails, items: list[Item]) -> list[Query]:
        """3. What to ask indexers for these wanted items. Default: "<title> <number>" each."""
        return [
            Query(f"{title.name} {_num(i.number)}" if i.number is not None else title.name)
            for i in items
        ]

    def parse(self, release_name: str) -> Parsed | None:
        """4. What a release name says; None when it isn't this kind's."""
        raise NotImplementedError

    def pick(self, files: list[File], items: list[Item]) -> dict[str, str]:
        """5. Which file is which item: {file path: item ref}. Default: with one item, the
        largest file of the kind's types; otherwise numbers in the file names."""
        mine = [f for f in files if _ext(f.path) in self.file_types]
        if not mine or not items:
            return {}
        if len(items) == 1:
            return {max(mine, key=lambda f: f.size).path: items[0].ref}
        out: dict[str, str] = {}
        for i in items:
            if i.number is None:
                continue
            want = re.compile(rf"(?<![\d.])0*{re.escape(_num(i.number))}(?![\d])")  # 061 is 61
            hits = [f for f in mine if f.path not in out and want.search(_stem(f.path))]
            if len(hits) == 1:
                out[hits[0].path] = i.ref
        return out

    def name_tokens(self, title: TitleDetails, item: Item) -> dict[str, object]:
        """6. The values the naming's tokens stand for, for this item's file."""
        raise NotImplementedError


REQUIRED = ("search", "title", "parse", "name_tokens")


def check_kind(cls: type[MediaKind]) -> None:
    """The checks a kind gets when it's declared (``fetchbox plugin check`` runs them too)."""
    problems: list[str] = []
    kid = cls.__dict__.get("id", "")
    if not isinstance(kid, str) or not KIND_ID.match(kid):
        problems.append(f"id {kid!r}: 2 to 10 lowercase letters, digits or _, from a letter")
    elif kid in CORE_KINDS:
        problems.append(f"id {kid!r} is a core kind: movies, TV and music are built in")
    if not getattr(cls, "name", "").strip():
        problems.append('name: the label people see ("Comics")')
    levels = getattr(cls, "levels", ())
    if not (isinstance(levels, tuple) and 1 <= len(levels) <= 3 and all(levels)):
        problems.append('levels: 1 to 3 words, title first, like ("series", "volume", "issue")')
    types = getattr(cls, "file_types", None) or set()
    bad = sorted(str(t) for t in types if not _good_ext(t))
    if not types or bad:
        why = f" (not {', '.join(bad)})" if bad else ""
        problems.append(f'file_types: lowercase extensions with the dot, like {{".cbz"}}{why}')
    if getattr(cls, "grab_level", "item") not in ("item", "group"):
        problems.append('grab_level: "item" or "group"')
    if getattr(cls, "grab_level", "item") == "group" and len(levels) < 3:
        problems.append('grab_level "group" needs three levels (a group to grab)')
    if not isinstance(getattr(cls, "qualities", None), Qualities):
        problems.append("qualities: Qualities([...]), best first")
    if not isinstance(getattr(cls, "naming", None), Naming):
        problems.append("naming: Naming(pattern, tokens={...})")
    for m in REQUIRED:
        if getattr(cls, m) is getattr(MediaKind, m):
            problems.append(f"{m}(): required")
    if problems:
        raise KindDeclarationError(f"{cls.__name__}: " + "; ".join(problems))


def _good_ext(t: object) -> bool:
    return isinstance(t, str) and len(t) > 1 and t.startswith(".") and t == t.lower()


def _num(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def _ext(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[1].lower() if "." in base.lstrip(".") else ""


def _stem(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base
