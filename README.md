# Fetchbox

Self-hosted media manager for movies and TV: discover a title, pick a quality profile, and
Fetchbox downloads and organizes it. Works out of the box with a built-in torrent engine.

Status: early development — see [PLAN.md](PLAN.md) and the engineering specification.

## Development

```sh
uv sync                     # Python 3.12 or 3.13
uv run fetchbox serve --data ./data   # run from the repo root
# API docs: http://127.0.0.1:8787/api/docs
uv run pytest
uv run ruff check . && uv run mypy && uv run lint-imports
```

The web UI lives in `web/` (SvelteKit, Node 24). `npm run build` writes it into
`src/fetchbox/static`, which `fetchbox serve` then serves at http://127.0.0.1:8787/.

```sh
cd web && npm ci
npm run dev        # live-reloading UI on :5173, API proxied to a running fetchbox serve
npm run build      # build into the Python package
npm run check      # type-check against the API's generated types
npm run gen        # after an API change: regenerate openapi.json + src/lib/schema.d.ts
```

License: MIT.
