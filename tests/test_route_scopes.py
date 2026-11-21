"""Every API route must declare its authorization: require(scope), authenticated() or public()."""

from __future__ import annotations

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute

from fetchbox.app import API_PREFIX, API_ROUTERS, create_app
from fetchbox.config import Settings
from fetchbox.modules.auth.deps import Require, RequireWS
from fetchbox.modules.auth.scopes import CATALOG

# Routes allowed to be public. Adding to this list is a security decision; review it.
PUBLIC = {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/setup"),
    ("GET", "/api/v1/auth/status"),  # only says whether first-run setup is pending
    ("GET", "/api/v1/system/ping"),
    # Not open: it checks the desktop tray's per-launch token itself, and otherwise
    # requires system:write (test_tray.py covers both, and the 401 without either).
    ("POST", "/api/v1/system/shutdown"),
}


def _methods(route: APIRoute | APIWebSocketRoute) -> set[str]:
    return route.methods if isinstance(route, APIRoute) else {"WEBSOCKET"}


def _requires(dep: Dependant) -> list[Require]:
    found = [dep.call] if isinstance(dep.call, Require) else []
    for sub in dep.dependencies:
        found.extend(_requires(sub))
    return found  # type: ignore[return-value]


def _api_routes(settings: Settings) -> list[APIRoute | APIWebSocketRoute]:
    """Routes from our registered routers, cross-checked against the OpenAPI document.

    `app.routes` can't be walked directly (FastAPI nests included routers lazily), and the
    OpenAPI document is the public, complete list of operations. Every operation it lists
    must map to a route we can inspect, so nothing escapes the check.
    """
    routes: list[APIRoute | APIWebSocketRoute] = [
        r
        for router in API_ROUTERS
        for r in router.routes
        if isinstance(r, APIRoute | APIWebSocketRoute)
    ]
    ours = {(m, API_PREFIX + r.path) for r in routes for m in _methods(r)}
    spec = create_app(settings).openapi()
    documented = {
        (method.upper(), path)
        for path, ops in spec["paths"].items()
        for method in ops
        if path.startswith("/api/")
    }
    assert documented, "OpenAPI lists no /api operations"
    assert documented <= ours, f"operations outside API_ROUTERS: {sorted(documented - ours)}"
    return routes  # includes include_in_schema=False routes: hidden ones still need auth


def test_every_route_declares_exactly_one_authorization(settings: Settings) -> None:
    missing, multiple = [], []
    for route in _api_routes(settings):
        reqs = _requires(route.dependant)
        label = f"{sorted(_methods(route))} {API_PREFIX}{route.path}"
        if not reqs:
            missing.append(label)
        elif len(reqs) > 1:
            multiple.append(f"{label}: {reqs}")
    assert len(_api_routes(settings)) >= 15  # guard against an empty walk passing vacuously
    assert not missing, f"routes without require()/authenticated()/public(): {missing}"
    assert not multiple, f"routes with several authorization markers: {multiple}"


def test_public_routes_are_allowlisted(settings: Settings) -> None:
    public = {
        (m, API_PREFIX + r.path)
        for r in _api_routes(settings)
        for m in _methods(r)
        if any(q.scope is None for q in _requires(r.dependant))
    }
    assert public == PUBLIC


def test_required_scopes_exist_in_catalog(settings: Settings) -> None:
    for route in _api_routes(settings):
        for q in _requires(route.dependant):
            if q.scope:
                assert q.scope in CATALOG, f"{route.path} requires unknown scope {q.scope!r}"


def test_websocket_routes_use_ws_require(settings: Settings) -> None:
    ws = [r for r in _api_routes(settings) if isinstance(r, APIWebSocketRoute)]
    assert ws, "expected at least /events/ws"
    for r in ws:
        reqs = _requires(r.dependant)
        assert reqs and all(isinstance(q, RequireWS) and q.scope for q in reqs), r.path


def test_openapi_documents_security_schemes(settings: Settings) -> None:
    schemes = create_app(settings).openapi()["components"]["securitySchemes"]
    assert {"HTTPBearer", "APIKeyHeader", "APIKeyCookie"} <= set(schemes)
