"""Serve the web UI: the SvelteKit build in ``fetchbox/static`` (``web/``, built with
``npm run build``). Unknown paths outside ``/api`` get ``index.html`` so the SPA's own
router handles deep links. Without a build, a short page says how to make one."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.exceptions import HTTPException

STATIC_DIR = Path(__file__).parent / "static"
IMMUTABLE = "public, max-age=31536000, immutable"

NOT_BUILT = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Fetchbox</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;
color:#1f2328;background:#fff}@media(prefers-color-scheme:dark){body{color:#e6edf3;
background:#0d1117}}code{background:rgba(127,127,127,.15);padding:.1rem .3rem;border-radius:4px}
</style></head><body><h1>Fetchbox is running</h1><p>The web UI hasn't been built in this
copy. Build it with <code>cd web &amp;&amp; npm ci &amp;&amp; npm run build</code>, or use
the API: <a href="/api/docs">/api/docs</a>.</p></body></html>"""


def install_ui(app: FastAPI, static_dir: Path = STATIC_DIR) -> None:
    root = static_dir.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def ui(path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404)  # the API's usual not-found problem document
        index = root / "index.html"
        if not index.is_file():
            return HTMLResponse(NOT_BUILT)
        target = (root / path).resolve()
        if path and target.is_file() and target.is_relative_to(root):
            headers = {"Cache-Control": IMMUTABLE} if "/immutable/" in f"/{path}" else None
            return FileResponse(target, headers=headers)
        # The SPA shell: never cached, so a new version is picked up straight away.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
