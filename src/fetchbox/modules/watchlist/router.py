from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from fetchbox.core.errors import problem_responses as pr
from fetchbox.modules.auth.deps import CurrentPrincipal, require
from fetchbox.modules.watchlist.models import PlexAccount, TraktUser
from fetchbox.modules.watchlist.service import SYNC_JOB, WatchlistService

router = APIRouter(prefix="/watchlist", tags=["watchlist"])
_read = [Depends(require("integrations:read"))]
_write = [Depends(require("integrations:write"))]

ItemState = Literal["added", "waiting", "in_library", "dismissed", "skipped"]


def _svc(request: Request) -> WatchlistService:
    svc: WatchlistService = request.app.state.watchlist
    return svc


class AccountOut(BaseModel):
    id: str
    username: str
    enabled: bool
    last_sync_at: datetime | None
    last_error: str | None = Field(description="Why the last read of its watchlist failed")


def _account(a: PlexAccount) -> AccountOut:
    return AccountOut(id=a.id, username=a.username, enabled=a.enabled,
                      last_sync_at=a.last_sync_at, last_error=a.last_error)  # fmt: skip


class ItemOut(BaseModel):
    id: str
    account: str = Field(description="Whose watchlist it's on")
    source: Literal["plex", "trakt"] = Field(description="Plex or Trakt")
    kind: Literal["movie", "series"]
    name: str
    year: int | None
    tmdb_id: int | None
    state: ItemState = Field(
        description="added (and searched for), waiting (for approval), in_library (it was "
        "already there), dismissed, or skipped (reason says why)"
    )
    reason: str | None
    title_id: str | None = Field(description="Its title in the library, once there")
    seen_at: datetime
    updated_at: datetime


class SignInOut(BaseModel):
    pin_id: int
    url: str = Field(description="Open this: Plex's page where the person approves Fetchbox")
    expires_in: int | None = Field(description="Seconds the sign-in stays open")


class SignInStatus(BaseModel):
    done: bool
    account: AccountOut | None = None


class AccountPatch(BaseModel):
    enabled: bool


class TraktUserOut(BaseModel):
    id: str
    username: str
    enabled: bool
    last_sync_at: datetime | None
    last_error: str | None


class TraktUserIn(BaseModel):
    username: str = Field(min_length=1, max_length=100, description="A Trakt username")


def _trakt(u: TraktUser) -> TraktUserOut:
    return TraktUserOut(id=u.id, username=u.username, enabled=u.enabled,
                        last_sync_at=u.last_sync_at, last_error=u.last_error)  # fmt: skip


class FromServerIn(BaseModel):
    server_id: str = Field(description="A Plex server under Media servers")


@router.get("/accounts", response_model=list[AccountOut], dependencies=_read)
async def list_accounts(request: Request) -> list[AccountOut]:
    """The Plex accounts whose watchlists are followed."""
    return [_account(a) for a in await _svc(request).accounts()]


@router.post("/sign-in", response_model=SignInOut, dependencies=_write, responses=pr(502))
async def start_sign_in(request: Request) -> SignInOut:
    """Start signing in with Plex: open `url`, approve Fetchbox there, then poll
    `GET /watchlist/sign-in/{pin_id}` until it's done."""
    return SignInOut(**await _svc(request).start_sign_in())


@router.get(
    "/sign-in/{pin_id}", response_model=SignInStatus, dependencies=_write, responses=pr(502)
)
async def finish_sign_in(pin_id: int, request: Request, p: CurrentPrincipal) -> SignInStatus:
    """Done once the person approved Fetchbox on plex.tv: the account is then followed."""
    account = await _svc(request).finish_sign_in(pin_id, actor=p.actor())
    return SignInStatus(done=account is not None, account=_account(account) if account else None)


@router.post(
    "/accounts/from-server", response_model=AccountOut, status_code=201, dependencies=_write,
    responses=pr(404, 409, 502),
)  # fmt: skip
async def from_server(body: FromServerIn, request: Request, p: CurrentPrincipal) -> AccountOut:
    """Follow the watchlist of the Plex account a media server was added with, with the
    token Fetchbox already has for it: no sign-in."""
    return _account(await _svc(request).from_server(body.server_id, actor=p.actor()))


@router.patch(
    "/accounts/{account_id}", response_model=AccountOut, dependencies=_write, responses=pr(404)
)
async def update_account(account_id: str, body: AccountPatch, request: Request) -> AccountOut:
    """Follow its watchlist, or pause that."""
    return _account(await _svc(request).set_enabled(account_id, body.enabled))


@router.delete("/accounts/{account_id}", status_code=204, dependencies=_write, responses=pr(404))
async def remove_account(account_id: str, request: Request, p: CurrentPrincipal) -> Response:
    """Stop following it. What its watchlist added stays in the library."""
    await _svc(request).remove_account(account_id, actor=p.actor())
    return Response(status_code=204)


@router.get("/trakt", response_model=list[TraktUserOut], dependencies=_read)
async def list_trakt(request: Request) -> list[TraktUserOut]:
    """The Trakt users whose watchlists are followed."""
    return [_trakt(u) for u in await _svc(request).trakt_users()]


@router.post(
    "/trakt", response_model=TraktUserOut, status_code=201, dependencies=_write,
    responses=pr(409, 502),
)  # fmt: skip
async def add_trakt(body: TraktUserIn, request: Request, p: CurrentPrincipal) -> TraktUserOut:
    """Follow a Trakt user's watchlist (a public profile; needs the Trakt Client ID in the
    config, `watchlist.trakt_client_id`). It's checked with Trakt first."""
    return _trakt(await _svc(request).add_trakt_user(body.username, actor=p.actor()))


@router.patch(
    "/trakt/{user_id}", response_model=TraktUserOut, dependencies=_write, responses=pr(404)
)
async def update_trakt(user_id: str, body: AccountPatch, request: Request) -> TraktUserOut:
    return _trakt(await _svc(request).set_trakt_enabled(user_id, body.enabled))


@router.delete("/trakt/{user_id}", status_code=204, dependencies=_write, responses=pr(404))
async def remove_trakt(user_id: str, request: Request, p: CurrentPrincipal) -> Response:
    """Stop following it. What its watchlist added stays in the library."""
    await _svc(request).remove_trakt_user(user_id, actor=p.actor())
    return Response(status_code=204)


@router.get("/items", response_model=list[ItemOut], dependencies=_read)
async def list_items(
    request: Request,
    state: Annotated[list[ItemState] | None, Query(description="Only these states")] = None,
) -> list[ItemOut]:
    """Titles seen on the watchlists, newest first, and what became of each."""
    return [ItemOut(**i) for i in await _svc(request).items(states=list(state or []))]


@router.post(
    "/items/{item_id}/approve", response_model=ItemOut, dependencies=_write,
    responses=pr(404, 409),
)  # fmt: skip
async def approve(item_id: str, request: Request, p: CurrentPrincipal) -> ItemOut:
    """Add a waiting (or skipped) title to the library and search for it."""
    return ItemOut(**await _svc(request).approve(item_id, actor=p.actor()))


@router.post("/items/{item_id}/dismiss", status_code=204, dependencies=_write, responses=pr(404))
async def dismiss(item_id: str, request: Request) -> Response:
    """Not wanted: it isn't added, nor asked about again."""
    await _svc(request).dismiss(item_id)
    return Response(status_code=204)


@router.post("/sync", status_code=202, dependencies=_write)
async def sync_now(request: Request) -> dict[str, Any]:
    """Read the watchlists now (they're read every 10 minutes anyway)."""
    job = await request.app.state.scheduler.run_now(SYNC_JOB)
    return {"job_id": job.id if job else None}
