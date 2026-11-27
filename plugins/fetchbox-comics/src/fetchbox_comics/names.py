"""Reading comic release names: "Saga 061 (2023) (Digital) (Zone-Empire).cbz",
"The Walking Dead #193 (2019).cbr", "Saga v01 001-006 (2012).cbz".

Fetchbox calls this (through Comics.parse) for every release an indexer or RSS feed
returns, so it must be quick and never assume a name is a comic: anything it can't read is
None. tests/cases.txt is a table of real names and what each should read as; `fetchbox
plugin test` checks every line.
"""

from __future__ import annotations

import re

from fetchbox_plugin import Parsed

# The file type in a name is its quality: these names are Comics.qualities' names, which is
# how Fetchbox ranks releases and decides upgrades
QUALITY = {".cbz": "CBZ", ".cbr": "CBR", ".cb7": "CB7", ".pdf": "PDF"}
# Series name, an optional volume, the issue number or run, "(of 12)", the year
ISSUE = re.compile(
    r"^(?P<title>.+?)"
    r"(?:\s+v(?:ol(?:ume)?)?\.?\s*(?P<volume>\d{1,3}))?"  # v01, Vol. 1
    r"\s+#?(?P<first>\d{1,4}(?:\.\d)?)(?:\s*-\s*#?(?P<last>\d{1,4}))?"  # 061, #61, 001-006
    r"(?:\s+\(of\s+\d+\))?"  # (of 12)
    r"(?:\s+\((?P<year>\d{4})\))?",
    re.IGNORECASE,
)
# Collected editions and annuals: not single issues of the series, so never grabbed for one
NOT_ISSUES = re.compile(r"\b(tpb|trade paperback|omnibus|annual|hc|hardcover|sketchbook)\b", re.I)
# The scanner/release group at the end, "(Zone-Empire)": kept for the release's history
GROUP = re.compile(r"\((?P<group>[^()]*-[^()]*|[A-Za-z]+-Empire|Zone-Empire)\)\s*$")


def parse(release_name: str) -> Parsed | None:
    """Issues and issue runs; collected editions (TPB, omnibus) and annuals aren't issues.

    Fetchbox uses the result like this: `title` must match the series' name (case and
    punctuation aside), each of `numbers` that's a wanted issue is covered by the release
    (a run, "001-006", covers six), `quality` must be one the title accepts, and `year`
    rules out another series of the same name."""
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
        return None  # backwards, or a run too long to be real: not something to grab
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
