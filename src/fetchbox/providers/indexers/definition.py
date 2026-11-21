"""Indexer definitions: Fetchbox's own declarative format for torrent sites (YAML).

A definition says where a site is, how to ask it for releases, and how to read each
result; the ``definition`` provider runs it. Written from scratch for Fetchbox (no
Cardigann/Jackett syntax). Shape::

    schema: 1
    slug: example                    # [a-z0-9-], unique
    name: Example
    version: "1"                     # bump when the definition changes
    description: What it indexes
    links: [https://example.org]     # the only hosts it may contact (and subdomains)
    settings:                        # which settings the user fills in
      - {name: api_key, label: API key, required: true}
    options:                         # extra non-secret settings, with defaults
      - {name: min_size, label: Minimum size (MB), type: int, default: 0}
    rate_limit: 1.0                  # seconds between requests
    headers: {Accept: application/json}
    login:                           # optional: form login, cookies kept
      path: /login
      form: {user: "{{ settings.username }}", pass: "{{ settings.password }}"}
      success_cookie: session
    search:
      requests:                      # first one whose `when` holds and that finds
        - when: [movie, imdb]        # something wins
          path: /api/search
          params: {imdb: "{{ query.ids.imdb }}"}
        - path: /api/search
          params: {q: "{{ query.term }} {{ query.year }}"}
      response:
        type: json                   # json or xml
        rows: data.results           # json: dotted path; xml: ElementTree path
        fields:
          title: "{{ row.name }}"
          download_url: "{{ row.link }}"
          size: "{{ row.size | size }}"
    test_term: ubuntu                # what `test` searches for

Templates are ``{{ path | filter:arg | ... }}``. Paths start at ``query`` (term, year,
season, episode, kind, ids.imdb/tmdb/tvdb), ``settings`` (base_url, api_key, username,
password, cookie), ``options`` or ``row``. A template that is a single ``{{ }}`` keeps
the value's type; otherwise parts are joined as text (missing = empty). For XML rows a
path is an ElementTree path to a child's text, ``child@attr`` for an attribute, with
prefixes from ``response.namespaces``. Filters: lower, upper, strip, urlencode, quote,
int, float, size, date, first, zpad:N, default:X, replace:A:B, regex:PATTERN, join:SEP.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from importlib import resources
from typing import Any, ClassVar, Literal
from urllib.parse import quote_plus, urljoin, urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from fetchbox.core.providers import TestResult, registry
from fetchbox.providers.indexers import (
    IndexerError,
    IndexerProvider,
    ReleaseCandidate,
    SearchQuery,
    TorrentSource,
)
from fetchbox.providers.indexers.torznab import info_hash_from_magnet

SCHEMA = 1
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_BODY = 20 * 1024 * 1024
MAX_TORRENT = 10 * 1024 * 1024
MAX_YAML = 256 * 1024
SLOTS = ("base_url", "api_key", "username", "password", "cookie")
FIELDS = (
    "title", "guid", "info_hash", "download_url", "magnet_url", "info_url", "size",
    "seeders", "leechers", "published_at",
)  # fmt: skip
Condition = Literal["movie", "series", "term", "imdb", "tmdb", "tvdb", "season", "episode"]


class DefinitionError(ValueError):
    """A definition that can't be loaded; ``errors`` lists what is wrong."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


# ---- the format -------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingSpec(_Strict):
    name: Literal["base_url", "api_key", "username", "password", "cookie"]
    label: str = ""
    required: bool = False


class OptionSpec(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,40}$")
    label: str = ""
    type: Literal["str", "int", "float", "bool"] = "str"
    default: str | int | float | bool | None = None


class RequestSpec(_Strict):
    when: list[Condition] = []
    method: Literal["get", "post"] = "get"
    path: str = Field(min_length=1)
    params: dict[str, str | list[str]] = {}
    headers: dict[str, str] = {}


class ResponseSpec(_Strict):
    type: Literal["json", "xml"]
    rows: str = ""
    namespaces: dict[str, str] = {}
    error: str | None = Field(default=None, description="Path to an error message, if any")
    fields: dict[str, str]

    @model_validator(mode="after")
    def _fields(self) -> ResponseSpec:
        unknown = sorted(set(self.fields) - set(FIELDS))
        if unknown:
            raise ValueError(f"unknown fields {unknown}; known: {', '.join(FIELDS)}")
        if "title" not in self.fields:
            raise ValueError("fields.title is required")
        if not {"download_url", "magnet_url"} & set(self.fields):
            raise ValueError("fields need download_url or magnet_url")
        return self


class SearchSpec(_Strict):
    requests: list[RequestSpec] = Field(min_length=1)
    response: ResponseSpec


class LoginSpec(_Strict):
    method: Literal["get", "post"] = "post"
    path: str
    form: dict[str, str] = {}
    success_cookie: str | None = None


class Definition(_Strict):
    schema_: int = Field(alias="schema")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=20)
    description: str = ""
    language: str = "en"
    links: list[str] = Field(min_length=1)
    settings: list[SettingSpec] = []
    options: list[OptionSpec] = []
    capabilities: list[str] = []
    rate_limit: float = Field(default=1.0, ge=0, le=60)
    headers: dict[str, str] = {}
    login: LoginSpec | None = None
    search: SearchSpec
    rss: SearchSpec | None = None
    test_term: str = "test"

    @model_validator(mode="after")
    def _check(self) -> Definition:
        if self.schema_ != SCHEMA:
            raise ValueError(
                f"schema {self.schema_} is not supported (this version reads {SCHEMA})"
            )
        for link in self.links:
            p = urlsplit(link)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise ValueError(f"link {link!r} must be an http(s) URL")
        for spec in (self.search, self.rss):
            for t in spec.response.fields.values() if spec else ():
                compile_template(t)
            for r in spec.requests if spec else ():
                for v in r.params.values():
                    for t in v if isinstance(v, list) else [v]:
                        compile_template(t)
        return self

    @property
    def hosts(self) -> set[str]:
        return {urlsplit(link).hostname or "" for link in self.links}

    def allows(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)


def parse_definition(text: str) -> Definition:
    if len(text.encode()) > MAX_YAML:
        raise DefinitionError([f"definition is larger than {MAX_YAML // 1024} KB"])
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DefinitionError([f"not valid YAML: {exc}"]) from None
    if not isinstance(data, dict):
        raise DefinitionError(["a definition is a YAML mapping"])
    try:
        return Definition.model_validate(data)
    except ValidationError as exc:
        raise DefinitionError(
            [
                f"{'.'.join(str(p) for p in e['loc']) or 'definition'}: {e['msg']}"
                for e in exc.errors()
            ]
        ) from None


# ---- the catalog --------------------------------------------------------------------------------


class Catalog:
    """Definitions the provider can run, by slug. Built-ins ship in
    ``fetchbox/definitions``; the sources module adds user definitions from the DB."""

    def __init__(self) -> None:
        self._defs: dict[str, tuple[Definition, str]] = {}

    def put(self, d: Definition, text: str) -> None:
        self._defs[d.slug] = (d, text)

    def drop(self, slug: str) -> None:
        self._defs.pop(slug, None)

    def get(self, slug: str) -> Definition | None:
        hit = self._defs.get(slug)
        return hit[0] if hit else None

    def all(self) -> list[Definition]:
        return [d for d, _ in self._defs.values()]


catalog = Catalog()


def builtin_definitions() -> list[tuple[Definition, str]]:
    out = []
    for f in sorted(resources.files("fetchbox.definitions").iterdir(), key=lambda p: p.name):
        if f.name.endswith(".yaml"):
            text = f.read_text(encoding="utf-8")
            out.append((parse_definition(text), text))
    return out


# ---- templates -----------------------------------------------------------------------------------

_TAG = re.compile(r"\{\{(.*?)\}\}", re.S)
_ATTR = re.compile(r"@([A-Za-z_][\w.:-]*)$")
_SIZE = re.compile(r"^\s*([\d.,]+)\s*([kmgtp]?i?b?)?\s*$", re.I)
_UNITS = {"": 1, "b": 1, "k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12, "p": 1e15}


def _split(expr: str) -> list[str]:
    return [p.strip() for p in expr.split("|")]


def compile_template(t: str) -> None:
    """Raise ValueError for an unknown root or filter, at load time not search time."""
    for m in _TAG.finditer(t):
        path, *filters = _split(m.group(1))
        root = path.split(".", 1)[0]
        if root not in ("query", "settings", "options", "row"):
            raise ValueError(f"template {t!r}: paths start with query, settings, options or row")
        for f in filters:
            if f.split(":", 1)[0] not in FILTERS:
                raise ValueError(f"template {t!r}: unknown filter {f!r}")


def parse_size(v: Any) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, int | float):
        return int(v)
    m = _SIZE.match(str(v))
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    base = 1024 if "i" in unit else 1000
    power = "_kmgtp".find(unit[:1]) if unit[:1] in "kmgtp" and unit else 0
    return int(n * (base ** max(power, 0)))


def parse_date(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, int | float) or (isinstance(v, str) and v.isdigit()):
        return datetime.fromtimestamp(float(v), UTC)
    s = str(v).strip()
    with contextlib.suppress(ValueError):
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    with contextlib.suppress(TypeError, ValueError, IndexError):
        return parsedate_to_datetime(s)
    return None


def _num(v: Any, kind: type) -> Any:
    try:
        return kind(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None


def _regex(v: Any, pattern: str) -> Any:
    m = re.search(pattern, "" if v is None else str(v))
    if not m:
        return None
    return m.group(1) if m.groups() else m.group(0)


FILTERS: dict[str, Any] = {
    "lower": lambda v: None if v is None else str(v).lower(),
    "upper": lambda v: None if v is None else str(v).upper(),
    "strip": lambda v: None if v is None else str(v).strip(),
    "urlencode": lambda v: "" if v is None else quote_plus(str(v)),
    "quote": lambda v: None if v in (None, "") else '"' + str(v).replace('"', '\\"') + '"',
    "int": lambda v: _num(v, int),
    "float": lambda v: _num(v, float),
    "size": parse_size,
    "date": parse_date,
    "first": lambda v: (v[0] if v else None) if isinstance(v, list) else v,
    "zpad": lambda v, n="2": None if v in (None, "") else str(v).zfill(int(n)),
    "default": lambda v, d="": d if v in (None, "") else v,
    "replace": lambda v, a="", b="": None if v is None else str(v).replace(a, b),
    "regex": _regex,
    "join": lambda v, sep=",": sep.join(map(str, v)) if isinstance(v, list) else v,
}


class Context:
    def __init__(
        self,
        values: dict[str, Any],
        row: Any = None,
        namespaces: dict[str, str] | None = None,
    ) -> None:
        self.values = values
        self.row = row
        self.ns = namespaces or {}

    def lookup(self, path: str) -> Any:
        root, _, rest = path.partition(".")
        if root == "row":
            return (
                _xml_get(self.row, rest, self.ns)
                if isinstance(self.row, ET.Element)
                else (_json_get(self.row, rest))
            )
        return _json_get(self.values.get(root), rest)


def _json_get(v: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p] if path else []:
        if isinstance(v, dict):
            v = v.get(part)
        elif isinstance(v, list) and part.lstrip("-").isdigit():
            i = int(part)
            v = v[i] if -len(v) <= i < len(v) else None
        else:
            return None
    return v


def _xml_get(el: ET.Element | None, path: str, ns: dict[str, str]) -> Any:
    if el is None:
        return None
    node_path, attr = path, ""
    if (m := _ATTR.search(path)) is not None:  # a trailing @attr, not one in [@x='y']
        node_path, attr = path[: m.start()], m.group(1)
    node = el if node_path in ("", ".") else el.find(node_path, ns)
    if node is None:
        return None
    return node.get(attr) if attr else (node.text or "").strip()


def render(template: str, ctx: Context) -> Any:
    tags = list(_TAG.finditer(template))
    if len(tags) == 1 and tags[0].group(0) == template.strip():
        return _eval(tags[0].group(1), ctx)  # keeps the value's type

    def sub(m: re.Match[str]) -> str:
        v = _eval(m.group(1), ctx)
        return "" if v is None else str(v)

    return _TAG.sub(sub, template).strip()


def _eval(expr: str, ctx: Context) -> Any:
    path, *filters = _split(expr)
    v = ctx.lookup(path)
    for f in filters:
        name, *args = f.split(":")
        if name == "regex":  # the pattern may itself contain colons
            args = [f.split(":", 1)[1]] if ":" in f else [""]
        v = FILTERS[name](v, *args)
    return v


# ---- the provider ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Resp:
    status: int
    url: str
    location: str | None
    body: bytes


class DefinitionSettings(BaseModel):
    definition: str = Field(description="Slug of the indexer definition to run")
    base_url: str | None = Field(default=None, description="Mirror: one of the definition's links")
    api_key: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None
    cookie: SecretStr | None = Field(default=None, description="Cookie header for the site")
    options: dict[str, str | int | float | bool] = {}

    @model_validator(mode="after")
    def _against_definition(self) -> DefinitionSettings:
        d = catalog.get(self.definition)
        if d is None:
            known = ", ".join(sorted(x.slug for x in catalog.all())) or "none"
            raise ValueError(f"unknown definition {self.definition!r}; known: {known}")
        if self.base_url is not None:
            self.base_url = self.base_url.strip().rstrip("/")
            if self.base_url not in [link.rstrip("/") for link in d.links]:
                raise ValueError(f"base_url must be one of {', '.join(d.links)}")
        for s in d.settings:
            if s.required and getattr(self, s.name) in (None, ""):
                raise ValueError(f"{s.name} is required by {d.name}")
        known_opts = {o.name for o in d.options}
        if unknown := sorted(set(self.options) - known_opts):
            raise ValueError(f"unknown options {unknown} for {d.name}")
        return self


class DefinitionProvider(IndexerProvider):
    kind: ClassVar[str] = "definition"
    Settings: ClassVar[type[BaseModel]] = DefinitionSettings
    capabilities: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self, settings: BaseModel, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(settings)
        assert isinstance(settings, DefinitionSettings)
        d = catalog.get(settings.definition)
        if d is None:
            raise IndexerError("config", f"indexer definition {settings.definition!r} is gone")
        self.d = d
        self.cfg = settings
        self.base = (settings.base_url or d.links[0]).rstrip("/")
        headers = {"User-Agent": "Fetchbox", **d.headers}
        if settings.cookie is not None:
            headers["Cookie"] = settings.cookie.get_secret_value()
        self._http = httpx.AsyncClient(timeout=TIMEOUT, transport=transport, headers=headers)
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._logged_in = d.login is None

    async def aclose(self) -> None:
        await self._http.aclose()

    def _values(self, q: SearchQuery | None) -> dict[str, Any]:
        opts = {o.name: o.default for o in self.d.options} | self.cfg.options
        settings = {
            "base_url": self.base,
            "api_key": self.cfg.api_key.get_secret_value() if self.cfg.api_key else None,
            "username": self.cfg.username,
            "password": self.cfg.password.get_secret_value() if self.cfg.password else None,
        }
        query: dict[str, Any] = {}
        if q is not None:
            query = {
                "kind": q.kind,
                "term": q.terms[0] if q.terms else "",
                "year": q.year,
                "season": q.season,
                "episode": q.episode,
                "ids": dict(q.ids),
            }
        return {"query": query, "settings": settings, "options": opts}

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cap: int = MAX_BODY,
    ) -> _Resp:
        """One request, rate limited, following redirects only within declared hosts."""
        for _ in range(5):
            if not self.d.allows(url):
                raise IndexerError(
                    "invalid_response", f"{self.d.name} tried to reach an undeclared host: {url}"
                )
            async with self._lock:
                wait = self._last + self.d.rate_limit - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            req = self._http.build_request(method, url, params=params, data=data, headers=headers)
            try:
                r = await self._http.send(req, stream=True)
                try:
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > cap:
                            raise IndexerError("invalid_response", "response is too large")
                finally:
                    await r.aclose()
            except httpx.TimeoutException:
                raise IndexerError("unavailable", f"{self.d.name} timed out") from None
            except httpx.HTTPError as exc:
                raise IndexerError(
                    "unavailable", f"{self.d.name} unreachable: {type(exc).__name__}"
                ) from None
            name, code = self.d.name, r.status_code
            if r.is_redirect and (loc := r.headers.get("location")):
                target = urljoin(str(r.url), loc)
                if target.startswith("magnet:"):
                    return _Resp(code, str(r.url), target, bytes(body))
                url, method, params, data = target, "GET", None, None
                continue
            if code in (401, 403):
                raise IndexerError("auth", f"{name} refused the request (HTTP {code})")
            if code == 429:
                retry = _num(r.headers.get("Retry-After"), float)
                raise IndexerError("rate_limited", f"{name} rate limit hit", retry_after=retry)
            if code >= 400:
                raise IndexerError("unavailable", f"{name} returned HTTP {code}")
            return _Resp(code, str(r.url), None, bytes(body))
        raise IndexerError("invalid_response", "too many redirects")

    async def _login(self) -> None:
        if self._logged_in or self.d.login is None:
            return
        spec = self.d.login
        ctx = Context(self._values(None))
        form = {k: render(v, ctx) for k, v in spec.form.items()}
        url = self.base + "/" + spec.path.lstrip("/")
        if spec.method == "post":
            await self._send("POST", url, data=form)
        else:
            await self._send("GET", url, params=form)
        if spec.success_cookie and spec.success_cookie not in self._http.cookies:
            raise IndexerError("auth", f"login to {self.d.name} failed (check the credentials)")
        self._logged_in = True

    @staticmethod
    def _applies(r: RequestSpec, q: SearchQuery) -> bool:
        have = {
            "movie": q.kind == "movie",
            "series": q.kind == "series",
            "term": bool(q.terms and q.terms[0]),
            "imdb": bool(q.ids.get("imdb")),
            "tmdb": bool(q.ids.get("tmdb")),
            "tvdb": bool(q.ids.get("tvdb")),
            "season": q.season is not None,
            "episode": q.episode is not None,
        }
        return all(have[c] for c in r.when)

    async def _run(self, spec: SearchSpec, q: SearchQuery | None) -> list[ReleaseCandidate]:
        await self._login()
        values = self._values(q)
        for r in spec.requests:
            if q is not None and not self._applies(r, q):
                continue
            ctx = Context(values)
            params: dict[str, Any] = {}
            for k, v in r.params.items():
                rendered = [render(x, ctx) for x in v] if isinstance(v, list) else render(v, ctx)
                if rendered not in (None, "", []):
                    params[k] = rendered
            url = self.base + "/" + r.path.lstrip("/")
            resp = await self._send(
                r.method.upper(),
                url,
                params=params if r.method == "get" else None,
                data=params if r.method == "post" else None,
                headers=r.headers or None,
            )
            found = self._parse(spec.response, resp.body, values)
            if found:
                return found
        return []  # nothing found, or this site can't answer this kind of query

    def _rows(self, spec: ResponseSpec, body: bytes) -> tuple[list[Any], Any]:
        if spec.type == "json":
            try:
                doc = json.loads(body)
            except ValueError:
                raise IndexerError(
                    "invalid_response", f"{self.d.name} did not return JSON"
                ) from None
            rows = _json_get(doc, spec.rows)
            if rows is None:
                rows = []
            if isinstance(rows, dict):
                rows = list(rows.values())
            if not isinstance(rows, list):
                raise IndexerError("invalid_response", f"{spec.rows!r} is not a list")
            return rows, doc
        if b"<!DOCTYPE" in body[:2048] or b"<!ENTITY" in body:
            raise IndexerError("invalid_response", "XML with a DTD is refused")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise IndexerError("invalid_response", f"{self.d.name} sent bad XML: {exc}") from None
        return list(root.findall(spec.rows or ".", spec.namespaces)), root

    def _parse(
        self, spec: ResponseSpec, body: bytes, values: dict[str, Any]
    ) -> list[ReleaseCandidate]:
        rows, doc = self._rows(spec, body)
        if spec.error:
            err = (
                _json_get(doc, spec.error)
                if spec.type == "json"
                else _xml_get(doc, spec.error, spec.namespaces)
            )
            if err:
                raise IndexerError("unavailable", f"{self.d.name}: {err}")
        out = []
        for row in rows:
            ctx = Context(values, row, spec.namespaces)
            f = {k: render(t, ctx) for k, t in spec.fields.items()}
            title = str(f.get("title") or "").strip()
            magnet = f.get("magnet_url") or None
            link = f.get("download_url") or None
            if not title or not (magnet or link):
                continue
            if link and str(link).startswith("magnet:"):
                magnet, link = link, None
            if link:
                link = urljoin(self.base + "/", str(link))
            info = f.get("info_url")
            ih = str(f.get("info_hash") or "").lower() or info_hash_from_magnet(magnet)
            out.append(
                ReleaseCandidate(
                    title=title,
                    guid=str(f.get("guid") or link or magnet),
                    info_hash=ih if ih and len(ih) == 40 else None,
                    download_url=str(link) if link else None,
                    magnet_url=str(magnet) if magnet else None,
                    info_url=urljoin(self.base + "/", str(info)) if info else None,
                    size=parse_size(f.get("size")),
                    seeders=_num(f.get("seeders"), int),
                    leechers=_num(f.get("leechers"), int),
                    published_at=parse_date(f.get("published_at")),
                )
            )
        return out

    # ---- Provider / IndexerProvider ---------------------------------------------------------

    async def test(self) -> TestResult:
        q = SearchQuery("movie", (self.d.test_term,))
        try:
            found = await self._run(self.d.search, q)
        except IndexerError as exc:
            return TestResult(ok=False, message=exc.message, details={"kind": exc.kind})
        return TestResult(
            ok=True,
            message=f"{self.d.name} answered with {len(found)} result(s)",
            details={"definition": self.d.slug, "version": self.d.version},
        )

    async def search(self, q: SearchQuery) -> list[ReleaseCandidate]:
        return await self._run(self.d.search, q)

    async def rss(self) -> list[ReleaseCandidate]:
        return await self._run(self.d.rss, None) if self.d.rss else []

    async def resolve(self, c: ReleaseCandidate) -> TorrentSource:
        if c.magnet_url:
            return TorrentSource(magnet=c.magnet_url)
        if not c.download_url:
            raise IndexerError("invalid_response", "release has no usable download link")
        await self._login()
        r = await self._send("GET", c.download_url, cap=MAX_TORRENT)
        if r.location:  # redirected to a magnet link
            return TorrentSource(magnet=r.location)
        if not r.body.startswith(b"d"):
            raise IndexerError("invalid_response", "download is not a .torrent file")
        return TorrentSource(torrent=r.body)


for _d, _text in builtin_definitions():
    catalog.put(_d, _text)
registry.register(DefinitionProvider)
