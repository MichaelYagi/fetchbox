"""A tiny in-process Torznab endpoint for tests (httpx.MockTransport)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape

import httpx

API_KEY = "torznab-secret-key"

CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="Fake"/>
  <limits max="100" default="50"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,season,ep,tvdbid"/>
    <movie-search available="yes" supportedParams="q,imdbid"/>
  </searching>
  <categories>
    <category id="2000" name="Movies"><subcat id="2040" name="Movies/HD"/></category>
    <category id="5000" name="TV"/>
  </categories>
</caps>"""

GB = 1_000_000_000


@dataclass
class Item:
    title: str
    size: int | None = 4 * GB
    seeders: int | None = 50
    peers: int | None = 60
    infohash: str | None = None
    magnet: str | None = None
    link: str | None = None
    down: float | None = None
    up: float | None = None
    category: int = 2040
    extra: dict[str, str] = field(default_factory=dict)
    nzb: bool = False  # a Usenet post: its enclosure is an NZB

    def xml(self, n: int) -> str:
        attrs = {"category": str(self.category), **self.extra}
        if self.seeders is not None:
            attrs["seeders"] = str(self.seeders)
        if self.peers is not None:
            attrs["peers"] = str(self.peers)
        if self.infohash:
            attrs["infohash"] = self.infohash
        if self.magnet:
            attrs["magneturl"] = self.magnet
        if self.down is not None:
            attrs["downloadvolumefactor"] = str(self.down)
        if self.up is not None:
            attrs["uploadvolumefactor"] = str(self.up)
        link = self.link or f"http://fake/dl/{n}?apikey={API_KEY}"
        size = f"<size>{self.size}</size>" if self.size is not None else ""
        a = "".join(f'<torznab:attr name="{k}" value="{escape(v)}"/>' for k, v in attrs.items())
        return (
            f"<item><title>{escape(self.title)}</title><guid>http://fake/details/{n}</guid>"
            f"<link>{escape(link)}</link><comments>http://fake/details/{n}</comments>"
            f"<pubDate>Wed, 18 Nov 2026 10:00:00 +0000</pubDate>{size}"
            f'<enclosure url="{escape(link)}" length="{self.size or 0}" '
            f'type="{"application/x-nzb" if self.nzb else "application/x-bittorrent"}"/>'
            f"{a}</item>"
        )


def feed(items: list[Item]) -> str:
    body = "".join(i.xml(n) for n, i in enumerate(items))
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" '
        'xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>'
        f"<title>Fake</title>{body}</channel></rss>"
    )


NZB = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
    b'<file poster="p@fetchbox.test" date="1795000000" subject="Dune &quot;dune.mkv&quot; yEnc">'
    b"<groups><group>alt.binaries.test</group></groups><segments>"
    b'<segment bytes="300000" number="1">a@fetchbox.test</segment></segments></file></nzb>'
)


class FakeTorznab:
    def __init__(self, items: list[Item] | None = None, *, key: str = API_KEY) -> None:
        self.items = items or []
        self.key = key
        self.requests: list[httpx.Request] = []
        self.fail: int | None = None  # HTTP status to return for API calls
        self.error_doc: tuple[str, str] | None = None  # (code, description)
        self.by_id_finds_nothing = False

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.path.startswith("/dl/") or "/dl/" in req.url.path:  # (via Prowlarr: /<id>/dl/)
            n = req.url.path.rsplit("/", 1)[1]
            if n == "redirect":
                return httpx.Response(302, headers={"location": "magnet:?xt=urn:btih:" + "c" * 40})
            if any(i.nzb for i in self.items):
                return httpx.Response(
                    200, content=NZB, headers={"content-type": "application/x-nzb"}
                )
            return httpx.Response(200, content=b"d8:announce3:urle")
        if self.fail:
            return httpx.Response(self.fail, headers={"Retry-After": "120"})
        q = req.url.params
        if q.get("apikey") != self.key or self.error_doc:
            code, desc = self.error_doc or ("100", "Incorrect user credentials")
            return httpx.Response(200, text=f'<error code="{code}" description="{desc}"/>')
        t = q.get("t")
        if t == "caps":
            return httpx.Response(200, text=CAPS)
        if t == "movie" and "imdbid" in q and self.by_id_finds_nothing:
            return httpx.Response(200, text=feed([]))
        return httpx.Response(200, text=feed(self.items))

    def last(self, t: str) -> dict[str, Any]:
        req = next(r for r in reversed(self.requests) if r.url.params.get("t") == t)
        return dict(req.url.params)
