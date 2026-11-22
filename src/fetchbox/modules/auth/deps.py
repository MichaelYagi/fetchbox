"""Route authorization.

Every API route must depend on exactly one of:

- ``require("resource:action")``: authenticated principal holding that scope
- ``authenticated()``: any authenticated principal
- ``public()``: no credentials (login, first-run setup, liveness ping)
- ``ws_require("resource:action")``: the WebSocket equivalent of ``require``

``tests/test_route_scopes.py`` enumerates every route and fails if one has none.

Credentials, in order: ``Authorization: Bearer fb_...``, ``X-Api-Key: fb_...``, the
``fb_session`` cookie, then (event-stream routes only) a single-use ``?token=``.
Cookie-authenticated unsafe requests must echo the session's CSRF token in
``X-CSRF-Token``; cookie-authenticated WebSockets must come from the same origin.
"""

from __future__ import annotations

import dataclasses
import functools
import ipaddress
import os
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, Request, WebSocket, WebSocketException
from fastapi.security import APIKeyCookie, APIKeyHeader, HTTPBearer
from starlette.requests import HTTPConnection

from fetchbox.core.errors import ProblemError
from fetchbox.modules.auth.service import (
    AuthService,
    Principal,
    constant_time_equals,
    unauthorized,
)

SESSION_COOKIE = "fb_session"
CSRF_COOKIE = "fb_csrf"
CSRF_HEADER = "X-CSRF-Token"
STREAM_TOKEN_PARAM = "token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Close code for auth failures after a WebSocket handshake (4000-4999 = app-defined).
WS_UNAUTHORIZED = 4401
WS_FORBIDDEN = 4403

# Declared so OpenAPI documents the schemes; resolution is done by hand below.
_bearer = HTTPBearer(auto_error=False, description="API key: `fb_<prefix>_<secret>`")
_x_api_key = APIKeyHeader(name="X-Api-Key", auto_error=False)
_cookie = APIKeyCookie(name=SESSION_COOKIE, auto_error=False)


class AuthFailed(Exception):
    def __init__(self, status: int, detail: str, slug: str) -> None:
        super().__init__(detail)
        self.status, self.detail, self.slug = status, detail, slug


def get_auth(conn: HTTPConnection) -> AuthService:
    svc: AuthService = conn.app.state.auth
    return svc


# ---- reverse proxies (Settings > Network) ----------------------------------------------


def _network(conn: HTTPConnection) -> Any:
    cfg = getattr(conn.app.state, "config", None)
    return cfg.current.network if cfg is not None else None


@functools.lru_cache(maxsize=32)
def _nets(raw: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(ipaddress.ip_network(n, strict=False) for n in raw)


def _within(ip: str, nets: tuple[Any, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in n for n in nets)


def _peer(conn: HTTPConnection) -> str:
    return conn.client.host if conn.client else "unknown"


def from_trusted_proxy(conn: HTTPConnection) -> bool:
    net = _network(conn)
    return bool(net and net.trusted_proxies) and _within(
        _peer(conn), _nets(tuple(net.trusted_proxies))
    )


def client_ip(conn: HTTPConnection) -> str:
    """The client's address. Behind a trusted proxy: the last X-Forwarded-For hop that
    isn't itself a trusted proxy (earlier hops can be written by the client)."""
    peer = _peer(conn)
    if not from_trusted_proxy(conn):
        return peer
    nets = _nets(tuple(_network(conn).trusted_proxies))
    hops = [h.strip() for h in conn.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        if not _within(hop, nets):
            try:
                return str(ipaddress.ip_address(hop))
            except ValueError:
                return peer  # garbage in the header: don't log it as an address
    return hops[0] if hops else peer


def request_host(conn: HTTPConnection) -> str | None:
    """The host the browser used: X-Forwarded-Host from a trusted proxy (nginx often
    passes its upstream as Host), else Host."""
    if from_trusted_proxy(conn) and (fwd := conn.headers.get("x-forwarded-host")):
        return fwd.split(",")[0].strip()
    return conn.headers.get("host")


def _same_origin(conn: HTTPConnection) -> bool:
    origin = conn.headers.get("origin")
    if origin is None:
        # non-browser clients send no Origin; a browser that says cross-site is refused
        return conn.headers.get("sec-fetch-site") not in ("cross-site", "same-site")
    return urlsplit(origin).netloc == request_host(conn)


def _proxy_user(conn: HTTPConnection) -> str | None:
    """Who the trusted proxy's sign-in header names (Authelia, Authentik...), if set."""
    net = _network(conn)
    if not (net and net.auth_header) or not from_trusted_proxy(conn):
        return None
    who = conn.headers.get(net.auth_header, "").strip()
    return who[:64] or None


TRAY_ENV = "FETCHBOX_SUPERVISOR_TOKEN"
TRAY_HEADER = "x-fetchbox-supervisor"
TRAY_SCOPES = frozenset({"system:read", "system:write", "downloads:write"})


async def resolve_principal(
    conn: HTTPConnection, *, method: str | None, allow_stream_token: bool = False
) -> Principal | None:
    """Authenticate a request or WebSocket. `method=None` means WebSocket."""
    auth = get_auth(conn)
    ip = client_ip(conn)
    # The desktop tray: a random token it gave the server it started (in the server's
    # environment, gone with it), good for its own few actions only.
    tray = os.environ.get(TRAY_ENV)
    sent = conn.headers.get(TRAY_HEADER)
    if tray and sent:
        if not constant_time_equals(sent, tray):
            raise AuthFailed(401, "Invalid tray token", "unauthorized")
        return Principal(type="api_key", id="tray", name="tray", scopes=TRAY_SCOPES,
                         via="api_key", ip=ip)  # fmt: skip
    if (who := _proxy_user(conn)) is not None and "authorization" not in conn.headers \
            and "x-api-key" not in conn.headers:  # fmt: skip
        # The proxy signed this person in. It adds the header to every request the
        # browser makes, so other sites must not be able to make that browser write.
        if (method is None or method not in SAFE_METHODS) and not _same_origin(conn):
            raise AuthFailed(403, "Cross-origin request refused", "csrf")
        principal = await auth.proxy_principal(who)
        if principal is not None:
            return dataclasses.replace(principal, ip=ip)
    header = conn.headers.get("authorization", "")
    raw: str | None = None
    if header.lower().startswith("bearer "):
        raw = header[7:].strip()
    elif "x-api-key" in conn.headers:
        raw = conn.headers["x-api-key"].strip()
    if raw is not None:
        principal = await auth.resolve_api_key(raw, ip=ip)
        if principal is None:
            raise AuthFailed(401, "Invalid, expired or revoked API key", "unauthorized")
        return dataclasses.replace(principal, ip=ip)

    if token := conn.cookies.get(SESSION_COOKIE):
        principal = await auth.resolve_session(token)
        if principal is None:
            raise AuthFailed(401, "Session expired; sign in again", "unauthorized")
        if method is None:
            if not _same_origin(conn):
                raise AuthFailed(403, "Cross-origin WebSocket refused", "csrf")
        elif method not in SAFE_METHODS:
            sent = conn.headers.get(CSRF_HEADER, "")
            if not principal.csrf_token or not constant_time_equals(sent, principal.csrf_token):
                raise AuthFailed(403, "Missing or invalid CSRF token", "csrf")
        return dataclasses.replace(principal, ip=ip)

    if allow_stream_token and (st := conn.query_params.get(STREAM_TOKEN_PARAM)):
        principal = auth.redeem_stream_token(st)
        if principal is None:
            raise AuthFailed(401, "Stream token invalid, used or expired", "unauthorized")
        return dataclasses.replace(principal, ip=ip)
    return None


def _check(principal: Principal | None, scope: str) -> Principal:
    if principal is None:
        raise AuthFailed(401, "Missing or invalid credentials", "unauthorized")
    if scope and not principal.has(scope):
        raise AuthFailed(403, f"requires scope {scope!r}", "missing-scope")
    return principal


class Require:
    """HTTP dependency marker + resolver. `scope=None` = public; `""` = any principal."""

    def __init__(self, scope: str | None, *, allow_stream_token: bool = False) -> None:
        self.scope = scope
        self.allow_stream_token = allow_stream_token

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.scope!r})"

    async def __call__(
        self,
        request: Request,
        _b: Annotated[object, Depends(_bearer)] = None,
        _k: Annotated[object, Depends(_x_api_key)] = None,
        _c: Annotated[object, Depends(_cookie)] = None,
    ) -> Principal | None:
        if self.scope is None:
            return None
        try:
            principal = _check(
                await resolve_principal(
                    request, method=request.method, allow_stream_token=self.allow_stream_token
                ),
                self.scope,
            )
        except AuthFailed as exc:
            if exc.status == 401:
                raise unauthorized(exc.detail) from None
            raise ProblemError(403, "Forbidden", exc.detail, slug=exc.slug) from None
        request.state.principal = principal
        return principal


class RequireWS(Require):
    """WebSocket variant. Failing the handshake makes the server answer HTTP 403."""

    async def __call__(self, websocket: WebSocket) -> Principal:  # type: ignore[override]
        try:
            principal = _check(
                await resolve_principal(
                    websocket, method=None, allow_stream_token=self.allow_stream_token
                ),
                self.scope or "",
            )
        except AuthFailed as exc:
            code = WS_UNAUTHORIZED if exc.status == 401 else WS_FORBIDDEN
            raise WebSocketException(code=code, reason=exc.detail) from None
        websocket.state.principal = principal
        return principal


def require(scope: str, *, allow_stream_token: bool = False) -> Require:
    if not scope:
        raise ValueError("use authenticated() for scope-less routes")
    return Require(scope, allow_stream_token=allow_stream_token)


def ws_require(scope: str) -> RequireWS:
    return RequireWS(scope, allow_stream_token=True)


def authenticated() -> Require:
    return Require("")


def public() -> Require:
    return Require(None)


def principal_of(conn: HTTPConnection) -> Principal:
    p: Principal | None = getattr(conn.state, "principal", None)
    if p is None:
        raise unauthorized()
    return p


CurrentPrincipal = Annotated[Principal, Depends(principal_of)]
