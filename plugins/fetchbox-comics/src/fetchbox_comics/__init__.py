"""Comics: a media kind for comic series and their issues, with metadata from ComicVine.

On the Add page a series is found by name (ComicVine); its issues are what's wanted and
downloaded, named "Saga (2012)/Saga #061 (2023).cbz" by default.
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

COMICS = (7030,)  # Newznab/Torznab: Books > Comics


class ComicsSettings(Settings):
    comicvine_key: Secret = Field(
        title="ComicVine API key", help="Free: sign up at comicvine.gamespot.com/api"
    )

    async def test(self, ctx: Any) -> str:
        return await ComicVine(ctx, self.comicvine_key.value).check()


def _number(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None  # "½", "Alpha": not a number to search by


class Comics(MediaKind):
    id = "comics"
    name = "Comics"
    levels = ("series", "issue")
    file_types = frozenset({".cbz", ".cbr", ".cb7", ".pdf"})
    qualities = Qualities(["CBZ", "CB7", "CBR", "PDF"])
    naming = Naming(
        "{series} ({start_year})/{series} #{issue:03} ({year})",
        tokens={"series", "start_year", "issue", "year", "title", "publisher"},
    )
    settings = ComicsSettings
    categories = COMICS

    def _cv(self, ctx: Any) -> ComicVine:
        return ComicVine(ctx, self.settings.comicvine_key.value)

    async def search(self, ctx: Any, text: str) -> list[Hit]:
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
        """Comic releases write issue numbers with three digits: "Saga 061"."""
        out = []
        for i in items:
            n = i.number
            num = f"{int(n):03}" if n is not None and float(n).is_integer() else str(n or "")
            out.append(Query(f"{title.name} {num}".strip(), COMICS))
        return out

    def parse(self, release_name: str) -> Parsed | None:
        return parse(release_name)

    def name_tokens(self, title: TitleDetails, item: Item) -> dict[str, object]:
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
    id = "comics"
    name = "Comics"
    description = "Comic series and issues from ComicVine, filed as CBZ or CBR."
    fetchbox_api = 1
    homepage = "https://github.com/MichaelYagi/fetchbox/tree/main/plugins/fetchbox-comics"
    permissions: ClassVar[set[Permission]] = {Permission.NETWORK}
    contributes: ClassVar[list[type[Any]]] = [Comics]
