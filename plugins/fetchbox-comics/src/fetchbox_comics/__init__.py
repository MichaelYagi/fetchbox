"""Comics: a media kind for comic series and their issues, with metadata from ComicVine.

On the Add page a series is found by name (ComicVine); its issues are what's wanted and
downloaded, named "Saga (2012)/Saga #061 (2023).cbz" by default.

How it plugs into Fetchbox, start to finish (each place is marked "Fetchbox:" below):

1. Install: pyproject.toml's entry point ("fetchbox.plugins": comics = ...ComicsPlugin)
   is how Fetchbox finds the plugin in an installed wheel. ComicsPlugin says what it is,
   which plugin API it targets and what it uses (shown before it's turned on).
2. Settings: ComicsSettings becomes the form in Settings > Plugins (the key is a Secret:
   stored encrypted, never shown again); its test() is the Test button. Until the key is
   set the plugin doesn't start.
3. Turned on: Comics (a MediaKind) is registered: the Add page gets a Comics button, the
   Library a Comics tab, and a "Comics" library folder is made beside Movies the first
   time a series is added.
4. Add page: search() finds series by name; picking one calls title() for its issues.
   Fetchbox stores the series and makes each issue a wanted item (with its date, for the
   Calendar). The title page's Refresh calls title() again for new issues.
5. Searching (on request, and every 6 hours for what's missing): queries() says what to
   ask the indexers (in Settings > Sources) for; Fetchbox searches them in `categories`.
6. Choosing: parse() reads each release name the indexers return; Fetchbox keeps those
   for this series and a wanted issue, in a quality the title accepts (`qualities`, best
   first; the cutoff is chosen per title), and grabs the best. RSS feeds go the same way.
7. Downloaded: Fetchbox's default pick() matches the files to issues (by the issue
   numbers in their names, among `file_types`); name_tokens() fills in `naming` for each
   file's place in the library. Better releases replace files below the title's cutoff.

Everything here uses only the plugin API (fetchbox_plugin), never Fetchbox's internals,
so it keeps working across Fetchbox versions within API 1.
"""

from __future__ import annotations

from typing import Any, ClassVar

from fetchbox_comics.comicvine import VOLUME, ComicVine, _date, _year
from fetchbox_comics.names import parse
from fetchbox_plugin import (
    Field,
    Hit,
    Item,
    MediaKind,
    Naming,
    Parsed,
    Permission,
    Plugin,
    Qualities,
    Query,
    Secret,
    Settings,
    TitleDetails,
)

# Fetchbox: the indexer categories searched for this kind (Newznab/Torznab: Books > Comics).
# Indexers that don't carry the category return nothing for it.
COMICS = (7030,)


class ComicsSettings(Settings):
    """Fetchbox: drawn as the plugin's settings form in Settings > Plugins. Each field's
    title is its label and help the hint under it; unknown fields are refused, and the
    plugin isn't started until every required field (here, the key) is set."""

    # A Secret is stored encrypted and never sent back to the browser; read it with .value
    comicvine_key: Secret = Field(
        title="ComicVine API key", help="Free: sign up at comicvine.gamespot.com/api"
    )

    async def test(self, ctx: Any) -> str:
        """Fetchbox: the Test button. Return what worked; raise to show what didn't."""
        return await ComicVine(ctx, self.comicvine_key.value).check()


def _number(v: Any) -> float | None:
    """An issue's number as a number ("61" -> 61.0, "1.5" -> 1.5), or None for one that
    isn't (it can still be wanted, just not searched for by number)."""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None  # "½", "Alpha": not a number to search by


class Comics(MediaKind):
    """The media kind. The class attributes declare it; Fetchbox checks them when the
    class is defined (and `fetchbox plugin check` runs the same checks)."""

    # Fetchbox: the kind's id, permanent: it's stored with every comics title and file
    id = "comics"
    # Fetchbox: the Add page button and the Library tab
    name = "Comics"
    # Fetchbox: the words the UI uses for a title and its items ("series", "issue")
    levels = ("series", "issue")
    # Fetchbox: which downloaded files are this kind's (the rest of a torrent is ignored)
    file_types = frozenset({".cbz", ".cbr", ".cb7", ".pdf"})
    # Fetchbox: the qualities, best first. Each title chooses which it accepts and its
    # cutoff (the title page); a file below the cutoff is upgraded by a better release.
    # parse() reports a release's quality as one of these names.
    qualities = Qualities(["CBZ", "CB7", "CBR", "PDF"])
    # Fetchbox: where each file goes in the library folder; name_tokens() fills it in.
    # "{issue:03}" pads the number: Saga #061.
    naming = Naming(
        "{series} ({start_year})/{series} #{issue:03} ({year})",
        tokens={"series", "start_year", "issue", "year", "title", "publisher"},
    )
    # Fetchbox: `self.settings` holds the validated ComicsSettings before any method runs
    settings = ComicsSettings
    categories = COMICS

    def _cv(self, ctx: Any) -> ComicVine:
        return ComicVine(ctx, self.settings.comicvine_key.value)

    async def search(self, ctx: Any, text: str) -> list[Hit]:
        """Fetchbox: 1. the Add page. Series matching what someone typed. A Hit's `ref` is
        the plugin's own id for the series; Fetchbox gives it back to title()."""
        out = []
        for r in await self._cv(ctx).search(text):
            count = r.get("count_of_issues")
            publisher = (r.get("publisher") or {}).get("name")
            about = " · ".join(x for x in (publisher, f"{count} issues" if count else None) if x)
            out.append(Hit(
                ref=f"{VOLUME}-{r['id']}", name=str(r.get("name") or ""), year=_year(r.get("start_year")),  # noqa: E501
                overview=str(r.get("deck") or about), poster_url=(r.get("image") or {}).get("medium_url"),  # noqa: E501
            ))  # fmt: skip
        return out

    async def title(self, ctx: Any, ref: str) -> TitleDetails:
        """Fetchbox: 2. a series' details and issues, when it's added and on Refresh.
        Each Item becomes a wanted item; its `ref` (ComicVine's issue id) is how Fetchbox
        recognises the same issue on the next refresh (a new ref is a new issue, wanted as
        the title's monitor setting says), its `released` date puts it on the Calendar.
        `extra` is kept with the title for name_tokens() (the publisher)."""
        cv = self._cv(ctx)
        v = await cv.volume(ref)
        items = []
        for i in await cv.issues(int(ref.rsplit("-", 1)[-1])):
            n = _number(i.get("issue_number"))
            items.append(Item(
                ref=str(i["id"]), number=n, name=i.get("name"),
                released=_date(i.get("store_date")) or _date(i.get("cover_date")),
            ))  # fmt: skip
        items.sort(key=lambda it: (it.number is None, it.number or 0))
        return TitleDetails(
            ref=ref, name=str(v.get("name") or ""), year=_year(v.get("start_year")),
            overview=str(v.get("deck") or ""), poster_url=(v.get("image") or {}).get("medium_url"),
            items=items, extra={"publisher": (v.get("publisher") or {}).get("name")},
        )  # fmt: skip

    def queries(self, title: TitleDetails, items: list[Item]) -> list[Query]:
        """Fetchbox: 3. what to ask the indexers for these wanted issues (the 6-hourly
        search, and Search missing on the title page). The default would be "Saga 61";
        comic releases write issue numbers with three digits: "Saga 061"."""
        out = []
        for i in items:
            n = i.number
            num = f"{int(n):03}" if n is not None and float(n).is_integer() else str(n or "")
            out.append(Query(f"{title.name} {num}".strip(), COMICS))
        return out

    def parse(self, release_name: str) -> Parsed | None:
        """Fetchbox: 4. what a release name says, for every result the indexers return
        (and every RSS release): the series, issue number(s), year, quality. None when it
        isn't a comic issue at all. Fetchbox then keeps the ones for this series (by name),
        for issues it wants, in a quality the title accepts. A parser that raises is
        treated as "not this kind's"."""
        return parse(release_name)

    # 5. pick() isn't here: Fetchbox's default (the issue numbers in the file names, among
    # file_types) matches a download's files to its issues.

    def name_tokens(self, title: TitleDetails, item: Item) -> dict[str, object]:
        """Fetchbox: 6. the values `naming`'s tokens stand for, for one issue's file, when
        a download is filed in the library."""
        n = item.number
        return {
            "series": title.name,
            "start_year": title.year,
            "issue": int(n) if n is not None and float(n).is_integer() else n,
            "year": item.released.year if item.released else title.year,
            "title": item.name or "",
            "publisher": title.extra.get("publisher") or "",
        }


class ComicsPlugin(Plugin):
    """Fetchbox: the plugin itself, named by pyproject.toml's "fetchbox.plugins" entry
    point. Settings > Plugins shows its name, description and permissions (in plain words)
    before it's turned on; `contributes` is what it adds."""

    # Fetchbox: permanent; its settings and stored data hang off it
    id = "comics"
    name = "Comics"
    description = "Comic series and issues from ComicVine, filed as CBZ or CBR."
    # Fetchbox: the plugin API it's written for; a Fetchbox without API 1 refuses it
    fetchbox_api = 1
    homepage = "https://github.com/MichaelYagi/fetchbox/tree/main/plugins/fetchbox-comics"
    # Fetchbox: declared, shown when installing ("Uses the network"); `fetchbox plugin
    # check` compares them with what the code does
    permissions: ClassVar[set[Permission]] = {Permission.NETWORK}
    contributes: ClassVar[list[type[Any]]] = [Comics]
