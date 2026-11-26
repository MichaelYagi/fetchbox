"""Reading comic release names: "Saga 061 (2023) (Digital) (Zone-Empire).cbz",
"The Walking Dead #193 (2019).cbr", "Saga v01 001-006 (2012).cbz"."""

from __future__ import annotations

import re

from fetchbox_plugin import Parsed

QUALITY = {".cbz": "CBZ", ".cbr": "CBR", ".cb7": "CB7", ".pdf": "PDF"}
ISSUE = re.compile(
    r"^(?P<title>.+?)"
    r"(?:\s+v(?:ol(?:ume)?)?\.?\s*(?P<volume>\d{1,3}))?"  # v01, Vol. 1
    r"\s+#?(?P<first>\d{1,4}(?:\.\d)?)(?:\s*-\s*#?(?P<last>\d{1,4}))?"  # 061, #61, 001-006
    r"(?:\s+\(of\s+\d+\))?"  # (of 12)
    r"(?:\s+\((?P<year>\d{4})\))?",
    re.IGNORECASE,
)
NOT_ISSUES = re.compile(r"\b(tpb|trade paperback|omnibus|annual|hc|hardcover|sketchbook)\b", re.I)
GROUP = re.compile(r"\((?P<group>[^()]*-[^()]*|[A-Za-z]+-Empire|Zone-Empire)\)\s*$")


def parse(release_name: str) -> Parsed | None:
    """Issues and issue runs; collected editions (TPB, omnibus) and annuals aren't issues."""
    stem, dot, ext = release_name.rpartition(".")
    if not dot or f".{ext.lower()}" not in QUALITY:
        quality, stem = None, release_name  # an indexer's name, without a file type
    else:
        quality = QUALITY[f".{ext.lower()}"]
    if NOT_ISSUES.search(stem):
        return None
    m = ISSUE.match(stem.replace("_", " ").strip())
    if not m or not m["title"].strip():
        return None
    first = float(m["first"])
    last = float(m["last"]) if m["last"] else first
    if last < first or last - first > 200:
        return None
    numbers = (
        (first,) if first == last else tuple(float(n) for n in range(int(first), int(last) + 1))
    )
    group = GROUP.search(stem)
    return Parsed(
        title=m["title"].strip(" -"),
        numbers=numbers,
        year=int(m["year"]) if m["year"] else None,
        quality=quality,
        release_group=group["group"] if group else None,
    )
