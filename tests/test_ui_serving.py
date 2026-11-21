"""The web UI is served from the SvelteKit build, with deep links falling back to it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from asgi_lifespan import LifespanManager

from fetchbox.app import create_app


async def _get(settings: Any, static: Path, *paths: str) -> list[httpx.Response]:
    app = create_app(settings, on_bootstrap_key=lambda k: None, static_dir=static)
    async with LifespanManager(app, startup_timeout=60, shutdown_timeout=60):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return [await c.get(p) for p in paths]


async def test_serves_the_build_and_falls_back_to_index(settings: Any, tmp_path: Path) -> None:
    static = tmp_path / "static"
    (static / "_app" / "immutable").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>Fetchbox</title>shell")
    (static / "_app" / "immutable" / "app.123.js").write_text("console.log(1)")
    (static / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("outside")

    root, deep, asset, icon, escape, api, docs, status = await _get(
        settings, static, "/", "/titles/abc", "/_app/immutable/app.123.js", "/favicon.svg",
        "/../secret.txt", "/api/v1/nope", "/api/docs", "/api/v1/auth/status",
    )  # fmt: skip
    assert root.status_code == 200 and "shell" in root.text
    assert root.headers["cache-control"] == "no-cache"
    assert deep.status_code == 200 and "shell" in deep.text  # the SPA routes it
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    assert icon.status_code == 200 and icon.text == "<svg/>"
    assert "outside" not in escape.text
    assert api.status_code == 404 and api.headers["content-type"].startswith(
        "application/problem+json"
    )
    assert docs.status_code == 200 and "swagger" in docs.text.lower()
    assert status.status_code == 200 and status.json() == {"setup_required": True}


async def test_without_a_build_says_how_to_make_one(settings: Any, tmp_path: Path) -> None:
    [r] = await _get(settings, tmp_path / "nothing", "/")
    assert r.status_code == 200 and "npm run build" in r.text
