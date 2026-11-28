"""Plex watchlists: what's bookmarked in Plex arrives in the library.

People sign in with Plex (its PIN flow: no password reaches Fetchbox) and each signed-in
account's watchlist is read every few minutes. A movie or show seen there for the first
time is added with the configured quality and searched for, or, with "ask first", waits
for approval. Each title seen is remembered with what became of it, so one removed in
Fetchbox isn't added again just because it's still on the watchlist, and one already in
the library is left as it is.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import select

from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ConflictError, NotFoundError, ProblemError
from fetchbox.core.events import Actor, EventBus, Subject
from fetchbox.core.ids import new_id
from fetchbox.core.secrets import SecretBox
from fetchbox.modules.discovery.service import DiscoveryService
from fetchbox.modules.integrations.service import IntegrationsService
from fetchbox.modules.media.models import Title
from fetchbox.modules.media.service import MediaService
from fetchbox.modules.profiles.service import ProfilesService
from fetchbox.modules.system.config import ConfigService
from fetchbox.modules.watchlist.models import PlexAccount, TraktUser, WatchlistItem
from fetchbox.providers.media_servers.plex_tv import PlexTv, PlexTvError, WatchItem
from fetchbox.providers.media_servers.trakt import Trakt, TraktError

log = structlog.get_logger(__name__)

SYNC_JOB = "watchlist.sync"
SYSTEM = Actor(type="system", id="watchlist", name="Plex watchlist")


def _aad(account_id: str) -> str:
    return f"plex_account:{account_id}"


class WatchlistService:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        box: SecretBox,
        config: ConfigService,
        media: MediaService,
        discovery: DiscoveryService,
        profiles: ProfilesService,
        integrations: IntegrationsService,
    ) -> None:
        self.db = db
        self.bus = bus
        self.box = box
        self.config = config
        self.media = media
        self.discovery = discovery
        self.profiles = profiles
        self.integrations = integrations
        self.transport: httpx.AsyncBaseTransport | None = None  # (tests)

    async def _plex(self) -> PlexTv:
        cfg = self.config.current.watchlist
        if not cfg.client_id:  # this Fetchbox's id with Plex, made once and kept
            await self.config.update(
                {"watchlist": {"client_id": f"fetchbox-{uuid.uuid4()}"}}, if_match=None,
                actor=SYSTEM,
            )  # fmt: skip
        return PlexTv(self.config.current.watchlist.client_id, transport=self.transport)

    # ---- signing in ------------------------------------------------------------------------

    async def start_sign_in(self) -> dict[str, Any]:
        """A PIN, and the plex.tv page where the person approves Fetchbox."""
        plex = await self._plex()
        try:
            pin = await plex.create_pin()
            return {"pin_id": pin.id, "url": plex.auth_url(pin), "expires_in": pin.expires_in}
        except PlexTvError as exc:
            raise ProblemError(502, "Plex unavailable", detail=exc.message) from None
        finally:
            await plex.aclose()

    async def finish_sign_in(self, pin_id: int, *, actor: Actor) -> PlexAccount | None:
        """The account, once the PIN was approved on plex.tv; None while it isn't yet."""
        plex = await self._plex()
        try:
            pin = await plex.check_pin(pin_id)
            if pin.token is None:
                return None
            user = await plex.user(pin.token)
        except PlexTvError as exc:
            raise ProblemError(502, "Plex unavailable", detail=exc.message) from None
        finally:
            await plex.aclose()
        return await self._save_account(pin.token, user, actor)

    async def from_server(self, server_id: str, *, actor: Actor) -> PlexAccount:
        """Follow the watchlist of the account a Plex server was added with, using the
        token Fetchbox already has for it (no sign-in)."""
        _name, token = await self.integrations.plex_token(server_id)
        plex = await self._plex()
        try:
            user = await plex.user(token)
        except PlexTvError as exc:
            if exc.kind == "auth":
                raise ConflictError(
                    "Plex didn't accept that server's token for the watchlist (a server "
                    "token, not an account's): use Sign in with Plex instead"
                ) from None
            raise ProblemError(502, "Plex unavailable", detail=exc.message) from None
        finally:
            await plex.aclose()
        return await self._save_account(token, user, actor)

    async def _save_account(self, token: str, user: Any, actor: Actor) -> PlexAccount:
        async with self.db.sessionmaker() as s:
            row = (
                await s.execute(select(PlexAccount).where(PlexAccount.plex_id == user.id))
            ).scalar_one_or_none()
            if row is None:  # (signing in again replaces the token, keeps what it saw)
                row = PlexAccount(id=new_id(), plex_id=user.id, username=user.username)
                s.add(row)
            row.username, row.enabled, row.last_error = user.username, True, None
            row.token = self.box.encrypt(token, aad=_aad(row.id))
            await s.commit()
            await s.refresh(row)
        await self.bus.emit("watchlist.account_added", subject=Subject(type="plex_account",
                            id=row.id), actor=actor, name=row.username)  # fmt: skip
        return row

    async def accounts(self) -> list[PlexAccount]:
        async with self.db.sessionmaker() as s:
            q = select(PlexAccount).order_by(PlexAccount.created_at)
            return list((await s.execute(q)).scalars())

    async def set_enabled(self, account_id: str, enabled: bool) -> PlexAccount:
        async with self.db.sessionmaker() as s:
            row = await s.get(PlexAccount, account_id)
            if row is None:
                raise NotFoundError("plex_account", account_id)
            row.enabled = enabled
            await s.commit()
            await s.refresh(row)
            return row

    async def remove_account(self, account_id: str, *, actor: Actor) -> None:
        """Stop following it (what it added stays in the library)."""
        async with self.db.sessionmaker() as s:
            row = await s.get(PlexAccount, account_id)
            if row is None:
                raise NotFoundError("plex_account", account_id)
            name = row.username
            await s.delete(row)
            await s.commit()
        await self.bus.emit("watchlist.account_removed", subject=Subject(type="plex_account",
                            id=account_id), actor=actor, name=name)  # fmt: skip

    # ---- Trakt -------------------------------------------------------------------------------

    def _trakt(self) -> Trakt:
        key = self.config.current.watchlist.trakt_client_id
        if key is None:
            raise ConflictError(
                "add a Trakt Client ID first: make a free API app at trakt.tv/oauth/applications"
            )
        return Trakt(key.get_secret_value(), transport=self.transport)

    async def trakt_users(self) -> list[TraktUser]:
        async with self.db.sessionmaker() as s:
            q = select(TraktUser).order_by(TraktUser.created_at)
            return list((await s.execute(q)).scalars())

    async def add_trakt_user(self, username: str, *, actor: Actor) -> TraktUser:
        """Follow a Trakt user's watchlist (checked with Trakt first: it must be public)."""
        name = username.strip().lstrip("@")
        trakt = self._trakt()
        try:
            await trakt.watchlist(name)
        except TraktError as exc:
            if exc.kind == "unavailable":
                raise ProblemError(502, "Trakt unavailable", detail=exc.message) from None
            raise ConflictError(exc.message) from None
        finally:
            await trakt.aclose()
        async with self.db.sessionmaker() as s:
            if (await s.execute(select(TraktUser).where(TraktUser.username == name))).first():
                raise ConflictError(f"{name}'s Trakt watchlist is already followed")
            row = TraktUser(id=new_id(), username=name)
            s.add(row)
            await s.commit()
            await s.refresh(row)
        await self.bus.emit("watchlist.trakt_added", subject=Subject(type="trakt_user",
                            id=row.id), actor=actor, name=name)  # fmt: skip
        return row

    async def set_trakt_enabled(self, user_id: str, enabled: bool) -> TraktUser:
        async with self.db.sessionmaker() as s:
            row = await s.get(TraktUser, user_id)
            if row is None:
                raise NotFoundError("trakt_user", user_id)
            row.enabled = enabled
            await s.commit()
            await s.refresh(row)
            return row

    async def remove_trakt_user(self, user_id: str, *, actor: Actor) -> None:
        async with self.db.sessionmaker() as s:
            row = await s.get(TraktUser, user_id)
            if row is None:
                raise NotFoundError("trakt_user", user_id)
            name = row.username
            await s.delete(row)
            await s.commit()
        await self.bus.emit("watchlist.trakt_removed", subject=Subject(type="trakt_user",
                            id=user_id), actor=actor, name=name)  # fmt: skip

    async def _sync_trakt(self, counts: dict[str, int], actor: Actor) -> None:
        users = [u for u in await self.trakt_users() if u.enabled]
        if not users or self.config.current.watchlist.trakt_client_id is None:
            return
        trakt = self._trakt()
        try:
            for u in users:
                try:
                    found = await trakt.watchlist(u.username)
                    async with self.db.sessionmaker() as s:
                        known = set(
                            (
                                await s.execute(
                                    select(WatchlistItem.plex_key).where(
                                        WatchlistItem.trakt_user_id == u.id
                                    )
                                )
                            ).scalars()
                        )
                    for w in found:
                        if w.key in known:
                            continue
                        state = await self._new(
                            w, await self._tmdb_id(w), actor, owner=u.username,
                            source="Trakt", trakt_user_id=u.id,
                        )  # fmt: skip
                        counts[state] = counts.get(state, 0) + 1
                    await self._trakt_ok(u.id, None)
                except TraktError as exc:
                    counts["failed_accounts"] += 1
                    await self._trakt_ok(u.id, exc.message)
                    log.warning("watchlist.trakt_failed", user=u.username, error=exc.message)
        finally:
            await trakt.aclose()

    async def _trakt_ok(self, user_id: str, error: str | None) -> None:
        async with self.db.sessionmaker() as s:
            row = await s.get(TraktUser, user_id)
            if row is not None:
                row.last_sync_at, row.last_error = utcnow(), error
                await s.commit()

    # ---- the watchlist ---------------------------------------------------------------------

    async def items(
        self, *, states: list[str] | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        async with self.db.sessionmaker() as s:
            q = (
                select(WatchlistItem, PlexAccount.username, TraktUser.username)
                .outerjoin(PlexAccount, PlexAccount.id == WatchlistItem.account_id)
                .outerjoin(TraktUser, TraktUser.id == WatchlistItem.trakt_user_id)
                .order_by(WatchlistItem.updated_at.desc())
                .limit(limit)
            )
            if states:
                q = q.where(WatchlistItem.state.in_(states))
            rows = (await s.execute(q)).all()
        return [
            {"id": i.id, "account": plex or trakt or "", "source": "trakt" if trakt else "plex",
             "kind": i.kind, "name": i.name, "year": i.year, "tmdb_id": i.tmdb_id,
             "state": i.state, "reason": i.reason, "title_id": i.title_id, "seen_at": i.seen_at,
             "updated_at": i.updated_at}
            for i, plex, trakt in rows
        ]  # fmt: skip

    async def sync(self, *, actor: Actor = SYSTEM) -> dict[str, int]:
        """Read every followed watchlist; add what's new on it."""
        counts = {"added": 0, "waiting": 0, "in_library": 0, "skipped": 0, "failed_accounts": 0}
        await self._sync_trakt(counts, actor)
        accounts = [a for a in await self.accounts() if a.enabled]
        if not accounts:
            return counts
        plex = await self._plex()
        try:
            for account in accounts:
                try:
                    token = self.box.decrypt(account.token, aad=_aad(account.id))
                    found = await plex.watchlist(token)
                    async with self.db.sessionmaker() as s:
                        known = set(
                            (
                                await s.execute(
                                    select(WatchlistItem.plex_key).where(
                                        WatchlistItem.account_id == account.id
                                    )
                                )
                            ).scalars()
                        )
                    for w in found:
                        if w.key in known:
                            continue
                        tmdb_id = await self._tmdb_id(w, plex=plex, token=token)
                        state = await self._new(
                            w, tmdb_id, actor, owner=account.username, source="Plex",
                            account_id=account.id,
                        )  # fmt: skip
                        counts[state] = counts.get(state, 0) + 1
                    await self._account_ok(account.id, None)
                except PlexTvError as exc:
                    counts["failed_accounts"] += 1
                    await self._account_ok(account.id, exc.message)
                    log.warning(
                        "watchlist.sync_failed", account=account.username, error=exc.message
                    )
        finally:
            await plex.aclose()
        return counts

    async def _account_ok(self, account_id: str, error: str | None) -> None:
        async with self.db.sessionmaker() as s:
            row = await s.get(PlexAccount, account_id)
            if row is not None:
                row.last_sync_at, row.last_error = utcnow(), error
                await s.commit()

    async def _tmdb_id(
        self, w: WatchItem, *, plex: PlexTv | None = None, token: str = ""
    ) -> int | None:
        ids: dict[str, Any] = {"tmdb_id": w.tmdb_id, "imdb_id": w.imdb_id}
        if ids["tmdb_id"] is None and ids["imdb_id"] is None and plex is not None:
            ids.update(await plex.ids(token, w.key))  # (the listing didn't carry them)
        if ids.get("tmdb_id"):
            return int(ids["tmdb_id"])
        if ids.get("imdb_id") and self.discovery.configured:
            try:
                found = await self.discovery.provider().find_by_external_id("imdb", ids["imdb_id"])
            except Exception:
                return None
            if found is not None and found.kind == w.kind:
                return found.tmdb_id
        return None

    async def _new(
        self,
        w: WatchItem,
        tmdb_id: int | None,
        actor: Actor,
        *,
        owner: str,
        source: str,
        account_id: str | None = None,
        trakt_user_id: str | None = None,
    ) -> str:
        """A title seen on a watchlist for the first time: added, waiting, already there
        or skipped (and why)."""
        row = WatchlistItem(
            id=new_id(), account_id=account_id, trakt_user_id=trakt_user_id, plex_key=w.key,
            kind=w.kind, name=w.title, year=w.year, tmdb_id=tmdb_id, state="skipped",
        )  # fmt: skip
        if tmdb_id is None:
            row.reason = f"{source} doesn't say which TMDB title this is"
        elif (existing := await self._in_library(w.kind, tmdb_id)) is not None:
            row.state, row.title_id = "in_library", existing
        elif self.config.current.watchlist.ask_first:
            row.state = "waiting"
        else:
            row.state, row.reason, row.title_id = await self._add(row, actor)
        async with self.db.sessionmaker() as s:
            s.add(row)
            await s.commit()
        if row.state in ("added", "waiting"):
            await self.bus.emit(
                f"watchlist.{row.state}", subject=Subject(type="watchlist_item", id=row.id),
                actor=actor, name=w.title, year=w.year, account=owner, source=source,
                title_id=row.title_id,
            )  # fmt: skip
        return row.state

    async def _in_library(self, kind: str, tmdb_id: int) -> str | None:
        async with self.db.sessionmaker() as s:
            q = select(Title.id).where(Title.kind == kind, Title.tmdb_id == tmdb_id)
            return (await s.execute(q)).scalar_one_or_none()

    async def _profile(self, kind: str) -> str | None:
        cfg = self.config.current.watchlist
        chosen = cfg.series_profile_id if kind == "series" else cfg.movie_profile_id
        video = [p for p in await self.profiles.list_profiles() if p.media == "video"]
        if chosen and any(p.id == chosen for p in video):
            return chosen
        return video[0].id if video else None

    async def _add(self, item: WatchlistItem, actor: Actor) -> tuple[str, str | None, str | None]:
        """Add it to the library and search for it: (state, reason, title id)."""
        assert item.tmdb_id is not None
        profile = await self._profile(item.kind)
        if profile is None:
            return "skipped", "no quality profile for movies and shows", None
        try:
            if item.kind == "series":
                t = await self.media.add_series(
                    item.tmdb_id, profile_id=profile, actor=actor,
                    monitor_mode=self.config.current.watchlist.series_mode,
                )  # fmt: skip
            else:
                t = await self.media.add_movie(
                    item.tmdb_id, profile_id=profile, monitored=True, actor=actor
                )
        except ConflictError:  # added meanwhile, by hand
            return "in_library", None, await self._in_library(item.kind, item.tmdb_id)
        except Exception as exc:
            why = exc.problem.detail if isinstance(exc, ProblemError) else str(exc)
            return "skipped", f"couldn't add it: {why or exc}", None
        return "added", None, t.id

    async def approve(self, item_id: str, *, actor: Actor) -> dict[str, Any]:
        """A waiting (or skipped) title: add it now."""
        async with self.db.sessionmaker() as s:
            item = await s.get(WatchlistItem, item_id)
            if item is None:
                raise NotFoundError("watchlist_item", item_id)
            if item.tmdb_id is None:
                raise ConflictError("Plex doesn't say which TMDB title this is: add it by hand")
            item.state, item.reason, item.title_id = await self._add(item, actor)
            item.updated_at = utcnow()
            await s.commit()
            await s.refresh(item)
            state, name, year, title_id = item.state, item.name, item.year, item.title_id
        out = next(i for i in await self.items() if i["id"] == item_id)
        if state == "added":
            source = "Trakt" if out["source"] == "trakt" else "Plex"
            await self.bus.emit("watchlist.added", subject=Subject(type="watchlist_item",
                                id=item_id), actor=actor, name=name, year=year,
                                account=out["account"], source=source,
                                title_id=title_id)  # fmt: skip
        return out

    async def dismiss(self, item_id: str) -> None:
        """Not wanted: it stays off the library, and isn't asked about again."""
        async with self.db.sessionmaker() as s:
            item = await s.get(WatchlistItem, item_id)
            if item is None:
                raise NotFoundError("watchlist_item", item_id)
            item.state, item.updated_at = "dismissed", utcnow()
            await s.commit()


def last_sync(accounts: list[PlexAccount]) -> datetime | None:
    times = [a.last_sync_at for a in accounts if a.last_sync_at]
    return max(times) if times else None
