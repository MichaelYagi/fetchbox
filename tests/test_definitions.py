"""YAML indexer definitions: the format, the engine, the API, the built-in ones."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from fetchbox.providers.indexers import IndexerError, SearchQuery
from fetchbox.providers.indexers.definition import (
    Context,
    DefinitionError,
    DefinitionProvider,
    DefinitionSettings,
    builtin_definitions,
    catalog,
    parse_definition,
    parse_size,
    render,
)
from tests.conftest import Running, running
from tests.pipeline import Fakes, _movie

HASH = "ab" * 20
SITE = """
schema: 1
slug: fake-site
name: Fake Site
version: "1"
links: [https://fake.example]
settings:
  - {name: api_key, label: API key, required: true}
options:
  - {name: limit, type: int, default: 50}
rate_limit: 0
search:
  requests:
    - when: [movie, imdb]
      path: /api/movies
      params: {imdb: "{{ query.ids.imdb }}", key: "{{ settings.api_key }}"}
    - path: /api/search
      params:
        q: "{{ query.term }} {{ query.year }}"
        limit: "{{ options.limit }}"
        key: "{{ settings.api_key }}"
  response:
    type: json
    rows: data.results
    error: error
    fields:
      title: "{{ row.name }}"
      info_hash: "{{ row.hash | lower }}"
      download_url: "{{ row.link }}"
      size: "{{ row.size | size }}"
      seeders: "{{ row.stats.seeds }}"
      published_at: "{{ row.added | date }}"
"""


def _site(req: httpx.Request) -> httpx.Response:
    assert req.url.host == "fake.example"
    if req.url.path.startswith("/api") and req.url.params.get("key") != "k":
        return httpx.Response(401)
    if req.url.path == "/api/movies":
        return httpx.Response(200, json={"data": {"results": []}})  # id search finds nothing
    if req.url.path == "/api/search":
        rows = [
            {
                "name": "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX",
                "hash": HASH.upper(),
                "link": "/dl/1.torrent",
                "size": "4.2 GB",
                "stats": {"seeds": "80"},
                "added": 1700000000,
            },
            {"name": "", "link": "/dl/2.torrent"},  # no title: skipped
        ]
        return httpx.Response(200, json={"data": {"results": rows}, "q": req.url.params["q"]})
    if req.url.path == "/dl/1.torrent":
        return httpx.Response(302, headers={"location": "https://cdn.fake.example/1.torrent"})
    if req.url.host == "fake.example" and req.url.path == "/evil":
        return httpx.Response(302, headers={"location": "https://elsewhere.example/x"})
    return httpx.Response(404)


@pytest.fixture
def fake_site() -> Any:
    d = parse_definition(SITE)
    catalog.put(d, SITE)
    yield d
    catalog.drop(d.slug)


def _provider(transport: Any, **settings: Any) -> DefinitionProvider:
    s = DefinitionSettings.model_validate({"definition": "fake-site", "api_key": "k", **settings})
    return DefinitionProvider(s, transport=httpx.MockTransport(transport))


# ---- the format ---------------------------------------------------------------------------------


def test_builtins_load() -> None:
    slugs = [d.slug for d, _ in builtin_definitions()]
    assert "internet-archive" in slugs
    assert catalog.get("internet-archive") is not None


def test_invalid_definitions_list_every_problem() -> None:
    with pytest.raises(DefinitionError) as e:
        parse_definition("schema: 2\nslug: Bad Slug\nname: x\nversion: '1'\nlinks: [ftp://x]\n")
    msgs = " | ".join(e.value.errors)
    assert "slug" in msgs and "search" in msgs
    with pytest.raises(DefinitionError, match="not valid YAML"):
        parse_definition("a: [")
    bad_filter = SITE.replace("| size", "| nonsense")
    with pytest.raises(DefinitionError, match="unknown filter"):
        parse_definition(bad_filter)
    bad_root = SITE.replace("{{ row.name }}", "{{ env.HOME }}")
    with pytest.raises(DefinitionError, match="paths start with"):
        parse_definition(bad_root)
    with pytest.raises(DefinitionError, match="unknown fields"):
        parse_definition(SITE.replace("seeders:", "seedz:"))


def test_templates() -> None:
    ctx = Context({"query": {"term": "Amélie", "year": 2001, "season": 3}}, {"n": "42", "t": [1]})
    assert render("{{ query.year }}", ctx) == 2001  # a lone tag keeps its type
    assert render("{{ query.term }} {{ query.year }}", ctx) == "Amélie 2001"
    assert render("{{ query.term | urlencode }}", ctx) == "Am%C3%A9lie"
    assert render("S{{ query.season | zpad:2 }}", ctx) == "S03"
    assert render("{{ query.missing | default:none }}", ctx) == "none"
    assert render("{{ row.n | int }}", ctx) == 42
    assert render("{{ row.t | first }}", ctx) == 1
    assert render("{{ query.term | regex:(m\\w+) }}", ctx) == "mélie"
    assert render("{{ query.term | quote }}", ctx) == '"Amélie"'
    assert render("x {{ query.nothing }} y", ctx) == "x  y"
    assert parse_size("1.5 GB") == 1_500_000_000 and parse_size("2 GiB") == 2 * 1024**3
    assert parse_size("700 MB") == 700_000_000 and parse_size(123) == 123
    assert parse_size("lots") is None


def test_settings_are_checked_against_the_definition(fake_site: Any) -> None:
    with pytest.raises(ValueError, match="api_key is required"):
        DefinitionSettings.model_validate({"definition": "fake-site"})
    with pytest.raises(ValueError, match="unknown definition"):
        DefinitionSettings.model_validate({"definition": "nope"})
    with pytest.raises(ValueError, match="base_url must be one of"):
        DefinitionSettings.model_validate(
            {"definition": "fake-site", "api_key": "k", "base_url": "https://other.example"}
        )
    with pytest.raises(ValueError, match="unknown options"):
        DefinitionSettings.model_validate(
            {"definition": "fake-site", "api_key": "k", "options": {"x": 1}}
        )


# ---- the engine ---------------------------------------------------------------------------------


async def test_search_falls_back_and_maps_fields(fake_site: Any) -> None:
    p = _provider(_site)
    q = SearchQuery("movie", ("Dune",), year=2021, ids={"imdb": "tt1160419"})
    [c] = await p.search(q)
    assert c.title == "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"
    assert c.info_hash == HASH and c.seeders == 80 and c.size == 4_200_000_000
    assert c.download_url == "https://fake.example/dl/1.torrent"
    assert c.published_at is not None and c.published_at.year == 2023
    await p.aclose()


async def test_redirects_stay_on_declared_hosts(fake_site: Any) -> None:
    def torrent(req: httpx.Request) -> httpx.Response:
        if req.url.host == "cdn.fake.example":  # a subdomain is allowed
            return httpx.Response(200, content=b"d8:announce0:e")
        return _site(req)

    p = _provider(torrent)
    [c] = await p.search(SearchQuery("movie", ("Dune",)))
    src = await p.resolve(c)
    assert src.torrent == b"d8:announce0:e"

    with pytest.raises(IndexerError, match="undeclared host"):
        await p._send("GET", "https://fake.example/evil")
    with pytest.raises(IndexerError, match="undeclared host"):
        await p._send("GET", "https://elsewhere.example/")
    await p.aclose()


async def test_errors_are_reported(fake_site: Any) -> None:
    p = _provider(_site, api_key="wrong")
    with pytest.raises(IndexerError) as e:
        await p.search(SearchQuery("movie", ("Dune",)))
    assert e.value.kind == "auth"
    result = await p.test()
    assert not result.ok and result.details["kind"] == "auth"

    def site_error(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "daily limit reached"})

    p2 = _provider(site_error)
    with pytest.raises(IndexerError, match="daily limit reached"):
        await p2.search(SearchQuery("movie", ("Dune",)))
    await p.aclose()
    await p2.aclose()


async def test_xml_responses_and_login() -> None:
    text = """
schema: 1
slug: xml-site
name: XML Site
version: "1"
links: [https://xml.example]
settings: [{name: username}, {name: password}]
rate_limit: 0
login:
  path: /login
  form: {u: "{{ settings.username }}", p: "{{ settings.password }}"}
  success_cookie: sid
search:
  requests: [{path: /rss, params: {q: "{{ query.term }}"}}]
  response:
    type: xml
    rows: channel/item
    namespaces: {t: "urn:t"}
    fields:
      title: "{{ row.title }}"
      magnet_url: "{{ row.enclosure@url }}"
      seeders: "{{ row.t:attr[@name='seeders']@value }}"
"""
    d = parse_definition(text)
    catalog.put(d, text)
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/login":
            ok = b"u=me" in req.content and b"p=pw" in req.content
            return httpx.Response(200, headers={"set-cookie": "sid=1; Path=/"} if ok else {})
        assert req.headers.get("cookie") == "sid=1"
        xml = (
            '<rss xmlns:t="urn:t"><channel><item><title>Dune.2021.1080p.WEB-DL-GRP</title>'
            f'<enclosure url="magnet:?xt=urn:btih:{HASH}"/>'
            '<t:attr name="seeders" value="12"/></item></channel></rss>'
        )
        return httpx.Response(200, content=xml.encode())

    s = DefinitionSettings.model_validate(
        {"definition": "xml-site", "username": "me", "password": "pw"}
    )
    p = DefinitionProvider(s, transport=httpx.MockTransport(handler))
    [c] = await p.search(SearchQuery("movie", ("Dune",)))
    assert c.magnet_url and c.info_hash == HASH and c.seeders == 12
    await p.search(SearchQuery("movie", ("Dune",)))
    assert calls == ["/login", "/rss", "/rss"]  # logged in once
    await p.aclose()

    bad = DefinitionSettings.model_validate(
        {"definition": "xml-site", "username": "me", "password": "no"}
    )
    p = DefinitionProvider(bad, transport=httpx.MockTransport(handler))
    with pytest.raises(IndexerError, match="login"):
        await p.search(SearchQuery("movie", ("Dune",)))
    await p.aclose()
    catalog.drop("xml-site")


# ---- the API ------------------------------------------------------------------------------------


async def test_definitions_api_and_search_through_it(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    server.app.state.sources.provider_kwargs = {"transport": httpx.MockTransport(_site)}
    defs = (await c.get("/indexer-definitions")).json()
    ia = next(d for d in defs if d["slug"] == "internet-archive")
    assert ia["source"] == "builtin" and ia["links"] == ["https://archive.org"]
    assert "yaml" in (await c.get("/indexer-definitions/internet-archive")).json()

    r = await c.post("/indexer-definitions", json={"yaml": "schema: 1\nslug: x\n"})
    assert r.status_code == 422 and len(r.json()["errors"]) >= 3
    r = await c.post("/indexer-definitions", json={"yaml": SITE})
    assert r.status_code == 201, r.text
    assert r.headers["Location"].endswith("/indexer-definitions/fake-site")
    assert r.json()["source"] == "user" and r.json()["yaml"] == SITE
    assert (await c.post("/indexer-definitions", json={"yaml": SITE})).status_code == 409

    body = {"kind": "definition", "name": "Fake", "settings": {"definition": "fake-site"}}
    r = await c.post("/indexers", json=body)
    assert r.status_code == 422 and "api_key is required" in json.dumps(r.json())
    body["settings"]["api_key"] = "k"
    r = await c.post("/indexers", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["settings"]["api_key"] == "********"
    idx = r.json()["id"]
    assert (await c.post(f"/indexers/{idx}/test")).json()["ok"] is True

    movie = await _movie(c)
    res = (await c.get("/releases", params={"wanted_item_id": movie["wanted"][0]["id"]})).json()
    [rel] = res["releases"]
    assert rel["accepted"] and rel["candidate"]["source_name"] == "Fake"

    assert (await c.delete("/indexer-definitions/fake-site")).status_code == 409  # in use
    assert (await c.delete("/indexer-definitions/internet-archive")).status_code == 409
    assert (await c.delete(f"/indexers/{idx}")).status_code == 204
    assert (await c.delete("/indexer-definitions/fake-site")).status_code == 204
    assert (await c.get("/indexer-definitions/fake-site")).status_code == 404
    catalog.drop("fake-site")


async def test_first_run_adds_the_builtin_indexers(settings: Any) -> None:
    async with running(settings.model_copy(update={"seed_indexers": True})) as srv:
        async with srv.client() as c:
            [ia] = (await c.get("/indexers")).json()
            assert ia["name"] == "Internet Archive" and ia["enabled"]
            assert ia["settings"]["definition"] == "internet-archive"
            assert (await c.delete(f"/indexers/{ia['id']}")).status_code == 204
        await srv.app.state.sources.sync_definitions(seed=True)  # not a first run any more
        async with srv.client() as c:
            assert (await c.get("/indexers")).json() == []


async def test_internet_archive_films_are_accepted_by_the_any_profile(
    c: httpx.AsyncClient, fakes: Fakes, server: Running
) -> None:
    """The built-in definition, end to end through the decision engine, against a canned
    archive.org answer. Its titles don't state a quality: "Any" takes them, 1080p
    rightly doesn't."""
    docs = [
        {"identifier": "notld_201610", "title": "Night of the Living Dead", "year": "1968",
         "item_size": 724700396, "publicdate": "2016-10-30T12:00:00Z"},
    ]  # fmt: skip

    def archive(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "archive.org" and req.url.path == "/advancedsearch.php"
        q = req.url.params["q"]
        found = docs if "Night of the Living Dead" in q else []
        return httpx.Response(200, json={"response": {"docs": found}})

    server.app.state.sources.provider_kwargs = {"transport": httpx.MockTransport(archive)}
    fakes.tmdb.movies[10331] = {
        **fakes.tmdb.movies[438631],
        "id": 10331, "title": "Night of the Living Dead", "original_title":
        "Night of the Living Dead", "release_date": "1968-10-01", "imdb_id": "tt0063350",
        "runtime": 96,
    }  # fmt: skip
    body = {"kind": "definition", "name": "IA", "settings": {"definition": "internet-archive"}}
    assert (await c.post("/indexers", json=body)).status_code == 201
    profiles = {p["name"]: p["id"] for p in (await c.get("/quality-profiles")).json()}
    for name, accepted in (("Any", True), ("1080p", False)):
        r = await c.post("/titles", json={"tmdb_id": 10331, "profile_id": profiles[name],
                                          "search": False})  # fmt: skip
        assert r.status_code == 201, r.text
        t = r.json()
        res = (await c.get("/releases", params={"wanted_item_id": t["wanted"][0]["id"]})).json()
        [rel] = res["releases"]
        assert rel["candidate"]["title"] == "Night of the Living Dead 1968"
        assert rel["candidate"]["download_url"] == (
            "https://archive.org/download/notld_201610/notld_201610_archive.torrent"
        )
        assert rel["accepted"] is accepted, rel["rejections"]
        assert (await c.delete(f"/titles/{t['id']}")).status_code == 204
