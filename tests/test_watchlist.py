"""Plex watchlists: signing in with Plex, and what's watchlisted arriving in the library.

plex.tv is faked: its PIN responses are shaped on the real ones (POST /api/v2/pins gives
an id, a code, expiresIn 1800 and authToken null until approved), and the watchlist on
Plex's Discover service as Plex documents it (MediaContainer.Metadata, each with a Guid
list of tmdb://, imdb://, tvdb:// ids)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select

from fetchbox.modules.watchlist.models import PlexAccount
from tests.conftest import Running
from tests.pipeline import Fakes, standard_hd

TOKEN = "plex-token-abc"


class FakePlexTv:
    def __init__(self) -> None:
        self.approved: dict[int, str] = {}
        self.revoked: set[str] = set()
        self.watchlists: dict[str, list[dict[str, Any]]] = {TOKEN: []}
        self.metadata: dict[str, list[dict[str, str]]] = {}
        self.users = {TOKEN: {"uuid": "u-1", "username": "michael"}}

    def handle(self, req: httpx.Request) -> httpx.Response:
        token = req.headers.get("x-plex-token")
        assert req.headers.get("x-plex-client-identifier", "").startswith("fetchbox-")
        path = req.url.path
        if req.url.host == "plex.tv" and path == "/api/v2/pins" and req.method == "POST":
            return httpx.Response(201, json={"id": 7, "code": "abcd1234", "authToken": None,
                                             "expiresIn": 1800, "trusted": False})  # fmt: skip
        if req.url.host == "plex.tv" and path.startswith("/api/v2/pins/"):
            pin = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"id": pin, "code": "abcd1234",
                                             "authToken": self.approved.get(pin)})  # fmt: skip
        if token is None or token in self.revoked:
            return httpx.Response(401)
        if req.url.host == "plex.tv" and path == "/api/v2/user":
            return httpx.Response(200, json=self.users[token])
        if path == "/library/sections/watchlist/all":
            rows = self.watchlists.get(token, [])
            start = int(req.url.params.get("X-Plex-Container-Start", 0))
            size = int(req.url.params.get("X-Plex-Container-Size", 100))
            page = rows[start : start + size]
            return httpx.Response(200, json={"MediaContainer": {
                "size": len(page), "totalSize": len(rows), "Metadata": page}})  # fmt: skip
        if path.startswith("/library/metadata/"):
            key = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [
                {"ratingKey": key, "Guid": self.metadata.get(key, [])}]}})  # fmt: skip
        return httpx.Response(404)


def _movie(key: str, title: str, year: int, tmdb: int | None = None) -> dict[str, Any]:
    guids = [{"id": f"tmdb://{tmdb}"}] if tmdb else []
    return {"ratingKey": key, "type": "movie", "title": title, "year": year,
            "guid": f"plex://movie/{key}", "Guid": guids}  # fmt: skip


@pytest.fixture
def plex(server: Running) -> FakePlexTv:
    fake = FakePlexTv()
    server.app.state.watchlist.transport = httpx.MockTransport(fake.handle)
    return fake


async def _sign_in(c: httpx.AsyncClient, plex: FakePlexTv) -> dict[str, Any]:
    r = await c.post("/watchlist/sign-in")
    assert r.status_code == 200, r.text
    start = r.json()
    assert start["url"].startswith("https://app.plex.tv/auth#?clientID=fetchbox-")
    assert "code=abcd1234" in start["url"] and start["expires_in"] == 1800
    pending = (await c.get(f"/watchlist/sign-in/{start['pin_id']}")).json()
    assert pending == {"done": False, "account": None}  # not approved on plex.tv yet
    plex.approved[start["pin_id"]] = TOKEN  # the person approves Fetchbox on plex.tv
    done = (await c.get(f"/watchlist/sign-in/{start['pin_id']}")).json()
    assert done["done"] and done["account"]["username"] == "michael"
    account: dict[str, Any] = done["account"]
    return account


async def _sync(c: httpx.AsyncClient, server: Running) -> dict[str, int]:
    out: dict[str, int] = await server.app.state.watchlist.sync()
    return out


async def test_sign_in_with_plex_keeps_the_token_sealed(
    c: httpx.AsyncClient, plex: FakePlexTv, server: Running
) -> None:
    account = await _sign_in(c, plex)
    assert [a["username"] for a in (await c.get("/watchlist/accounts")).json()] == ["michael"]
    async with server.app.state.fb.db.sessionmaker() as s:
        row = (await s.execute(select(PlexAccount))).scalar_one()
    assert TOKEN not in row.token  # sealed with the secrets key, never stored as is
    # the same Fetchbox id with Plex every time (made once, kept in the config)
    cid = (await c.get("/system/config")).json()["config"]["watchlist"]["client_id"]
    assert cid.startswith("fetchbox-")
    plex.approved.clear()  # (a new sign-in: the fake's PIN is 7 again)
    again = await _sign_in(c, plex)  # signing in again: the same account, a fresh token
    assert again["id"] == account["id"]
    assert len((await c.get("/watchlist/accounts")).json()) == 1


async def test_watchlisted_titles_are_added_and_searched(
    c: httpx.AsyncClient, fakes: Fakes, plex: FakePlexTv, server: Running
) -> None:
    hd = await standard_hd(c)
    await c.patch("/system/config", json={"watchlist": {"movie_profile_id": hd,
                                                        "series_profile_id": hd}})  # fmt: skip
    await _sign_in(c, plex)
    plex.watchlists[TOKEN] = [
        _movie("m-dune", "Dune", 2021, tmdb=438631),
        {"ratingKey": "s-sev", "type": "show", "title": "Severance", "year": 2022,
         "guid": "plex://show/s-sev"},  # no Guid in the listing: read from its metadata
        _movie("m-mystery", "Some Mystery", 1999),  # Plex knows no TMDB id for it
        {"ratingKey": "e-1", "type": "episode", "title": "An episode"},  # not a title
    ]  # fmt: skip
    plex.metadata["s-sev"] = [{"id": "tvdb://371980"}, {"id": "tmdb://95396"}]

    counts = await _sync(c, server)
    assert (counts["added"], counts["skipped"]) == (2, 1)
    titles = {t["name"]: t for t in (await c.get("/titles")).json()["items"]}
    assert set(titles) == {"Dune", "Severance"}
    dune, sev = titles["Dune"], titles["Severance"]
    assert dune["kind"] == "movie" and dune["profile_id"] == hd
    assert sev["kind"] == "series" and sev["monitor_mode"] == "all"
    w = (await c.get(f"/titles/{dune['id']}")).json()["wanted"][0]
    assert w["monitored"] and w["next_search_at"] is None  # searched for straight away
    items = {i["name"]: i for i in (await c.get("/watchlist/items")).json()}
    assert items["Dune"]["state"] == "added" and items["Dune"]["title_id"] == dune["id"]
    assert items["Some Mystery"]["state"] == "skipped"
    assert "TMDB" in (items["Some Mystery"]["reason"] or "")
    activity = str((await c.get("/activity")).json())
    assert "michael's Plex watchlist" in activity

    # read again: nothing new; and a title removed in Fetchbox isn't added back
    assert (await _sync(c, server))["added"] == 0
    assert (await c.delete(f"/titles/{dune['id']}")).status_code == 204
    assert (await _sync(c, server))["added"] == 0
    names = {t["name"] for t in (await c.get("/titles")).json()["items"]}
    assert names == {"Severance"}


async def test_already_in_the_library_is_left_as_it_is(
    c: httpx.AsyncClient, fakes: Fakes, plex: FakePlexTv, server: Running
) -> None:
    hd = await standard_hd(c)
    mine = (await c.post("/titles", json={"kind": "movie", "tmdb_id": 438631,
                                          "profile_id": hd})).json()  # fmt: skip
    await _sign_in(c, plex)
    plex.watchlists[TOKEN] = [_movie("m-dune", "Dune", 2021, tmdb=438631)]
    assert (await _sync(c, server))["in_library"] == 1
    [item] = (await c.get("/watchlist/items")).json()
    assert item["state"] == "in_library" and item["title_id"] == mine["id"]
    assert len((await c.get("/titles")).json()["items"]) == 1


async def test_ask_first_waits_for_approval(
    c: httpx.AsyncClient, fakes: Fakes, plex: FakePlexTv, server: Running
) -> None:
    await standard_hd(c)
    await c.patch("/system/config", json={"watchlist": {"ask_first": True}})
    await _sign_in(c, plex)
    plex.watchlists[TOKEN] = [
        _movie("m-dune", "Dune", 2021, tmdb=438631),
        _movie("m-other", "Dune: Part Two", 2024, tmdb=693134),
    ]
    assert (await _sync(c, server))["waiting"] == 2
    assert (await c.get("/titles")).json()["items"] == []  # nothing added yet
    waiting = {i["name"]: i for i in (await c.get("/watchlist/items",
                                                  params={"state": "waiting"})).json()}  # fmt: skip
    assert set(waiting) == {"Dune", "Dune: Part Two"}

    r = await c.post(f"/watchlist/items/{waiting['Dune']['id']}/approve")
    assert r.status_code == 200 and r.json()["state"] == "added"
    assert (
        await c.post(f"/watchlist/items/{waiting['Dune: Part Two']['id']}/dismiss")
    ).status_code == 204
    assert [t["name"] for t in (await c.get("/titles")).json()["items"]] == ["Dune"]
    assert (await c.get("/watchlist/items", params={"state": "waiting"})).json() == []
    assert (await _sync(c, server))["waiting"] == 0  # a dismissed title isn't asked again


async def test_a_revoked_sign_in_says_so(
    c: httpx.AsyncClient, fakes: Fakes, plex: FakePlexTv, server: Running
) -> None:
    await _sign_in(c, plex)
    plex.revoked.add(TOKEN)  # signed out of all devices on plex.tv, say
    assert (await _sync(c, server))["failed_accounts"] == 1
    [account] = (await c.get("/watchlist/accounts")).json()
    assert "sign in again" in (account["last_error"] or "")

    # paused, it isn't read; removed, it's gone (what it added stays)
    r = await c.patch(f"/watchlist/accounts/{account['id']}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert (await _sync(c, server))["failed_accounts"] == 0
    assert (await c.delete(f"/watchlist/accounts/{account['id']}")).status_code == 204
    assert (await c.get("/watchlist/accounts")).json() == []


async def test_following_with_the_plex_servers_token(
    c: httpx.AsyncClient, plex: FakePlexTv, server: Running
) -> None:
    """No sign-in: the token Plex was added with under Media servers reads the watchlist."""
    server.app.state.integrations.provider_kwargs = {
        "transport": httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    }
    r = await c.post("/media-servers", json={"kind": "plex", "name": "Plex",
                                             "settings": {"url": "http://plex:32400",
                                                          "token": TOKEN}})  # fmt: skip
    assert r.status_code == 201, r.text
    r = await c.post("/watchlist/accounts/from-server", json={"server_id": r.json()["id"]})
    assert r.status_code == 201, r.text
    assert r.json()["username"] == "michael"
    async with server.app.state.fb.db.sessionmaker() as s:
        row = (await s.execute(select(PlexAccount))).scalar_one()
    assert TOKEN not in row.token  # sealed, like a signed-in one

    plex.revoked.add(TOKEN)  # a token plex.tv won't take: told to sign in instead
    other = await c.post("/media-servers", json={"kind": "plex", "name": "Plex 2",
                                                 "settings": {"url": "http://plex2:32400",
                                                              "token": TOKEN}})  # fmt: skip
    r = await c.post("/watchlist/accounts/from-server", json={"server_id": other.json()["id"]})
    assert r.status_code == 409 and "Sign in with Plex" in r.text


# ---- Trakt ---------------------------------------------------------------------------------

TRAKT_ID = "trakt-client-id-0123456789abcdef"


class FakeTrakt:
    """api.trakt.tv as Trakt documents it: /users/{user}/watchlist/{movies|shows}, each row
    {"rank", "listed_at", "type", "movie"|"show": {"title", "year", "ids": {...}}}."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, list[dict[str, Any]]]] = {
            "tom": {"movies": [], "shows": []}
        }
        self.private: set[str] = set()

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert req.url.host == "api.trakt.tv" and req.headers["trakt-api-version"] == "2"
        if req.headers.get("trakt-api-key") != TRAKT_ID:
            return httpx.Response(403)
        parts = req.url.path.strip("/").split("/")  # users/{user}/watchlist/{kind}
        if len(parts) != 4 or parts[0] != "users" or parts[2] != "watchlist":
            return httpx.Response(404)
        user, kind = parts[1], parts[3]
        if user in self.private:
            return httpx.Response(401)
        if user not in self.users:
            return httpx.Response(404)
        return httpx.Response(200, json=self.users[user][kind])


def _trakt_movie(trakt: int, title: str, year: int, **ids: Any) -> dict[str, Any]:
    return {"rank": 1, "listed_at": "2026-11-28T10:00:00.000Z", "type": "movie",
            "movie": {"title": title, "year": year, "ids": {"trakt": trakt, **ids}}}  # fmt: skip


@pytest.fixture
def trakt(server: Running) -> FakeTrakt:
    fake = FakeTrakt()
    server.app.state.watchlist.transport = httpx.MockTransport(fake.handle)
    return fake


async def test_a_trakt_watchlist_is_followed_like_plexs(
    c: httpx.AsyncClient, fakes: Fakes, trakt: FakeTrakt, server: Running
) -> None:
    await standard_hd(c)
    r = await c.post("/watchlist/trakt", json={"username": "tom"})
    assert r.status_code == 409 and "Trakt Client ID" in r.text  # not set up yet

    await c.patch("/system/config", json={"watchlist": {"trakt_client_id": f" {TRAKT_ID}\n"}})
    cfg = (await c.get("/system/config")).json()["config"]["watchlist"]
    assert cfg["trakt_client_id"] == "********"  # kept as a secret
    for who, why in (("nobody", "no Trakt user"), ("hidden", "private")):
        if who == "hidden":
            trakt.private.add(who)
        r = await c.post("/watchlist/trakt", json={"username": who})
        assert r.status_code == 409 and why in r.text, r.text

    trakt.users["tom"]["movies"] = [
        _trakt_movie(1, "Dune", 2021, tmdb=438631, imdb="tt1160419", slug="dune-2021"),
        _trakt_movie(2, "No Ids Film", 1990),  # Trakt has no TMDB or IMDb id for it
    ]
    severance = {"title": "Severance", "year": 2022,
                 "ids": {"trakt": 3, "tmdb": 95396, "tvdb": 371980}}  # fmt: skip
    trakt.users["tom"]["shows"] = [{"rank": 2, "type": "show", "show": severance}]
    r = await c.post("/watchlist/trakt", json={"username": "@tom"})
    assert r.status_code == 201 and r.json()["username"] == "tom"
    assert (await c.post("/watchlist/trakt", json={"username": "tom"})).status_code == 409

    counts = await _sync(c, server)
    assert (counts["added"], counts["skipped"]) == (2, 1)
    names = {t["name"]: t["kind"] for t in (await c.get("/titles")).json()["items"]}
    assert names == {"Dune": "movie", "Severance": "series"}
    items = {i["name"]: i for i in (await c.get("/watchlist/items")).json()}
    assert items["Dune"]["source"] == "trakt" and items["Dune"]["account"] == "tom"
    assert "Trakt doesn't say" in (items["No Ids Film"]["reason"] or "")
    assert "tom's Trakt watchlist" in str((await c.get("/activity")).json())
    assert (await _sync(c, server))["added"] == 0  # nothing new

    # a Client ID Trakt won't take: the user says so, and nothing else breaks
    await c.patch("/system/config", json={"watchlist": {"trakt_client_id": "wrong"}})
    assert (await _sync(c, server))["failed_accounts"] == 1
    [tom] = (await c.get("/watchlist/trakt")).json()
    assert "Client ID" in (tom["last_error"] or "")
    r = await c.patch(f"/watchlist/trakt/{tom['id']}", json={"enabled": False})
    assert r.json()["enabled"] is False
    assert (await c.delete(f"/watchlist/trakt/{tom['id']}")).status_code == 204
    assert (await c.get("/watchlist/trakt")).json() == []
    assert len((await c.get("/titles")).json()["items"]) == 2  # what it added stays
