# Fetchbox

Self-hosted media manager for movies and TV: discover a title, pick a quality profile, and
Fetchbox downloads and organizes it. Works out of the box with a built-in torrent engine.

Status: early development — see [PLAN.md](PLAN.md) and the engineering specification.

## Getting started

```sh
uv sync
cd web && npm ci && npm run build && cd ..   # the web UI (Node 24); the build isn't checked in
uv run fetchbox serve --data ./data          # then open http://127.0.0.1:8787
```

On first run, choose the admin password in the browser. The terminal also prints a
one-time API key for scripts. Back up `data/secrets.key`: it encrypts stored passwords and
keys.

### What you need

| | Needed for | Where to get it |
| --- | --- | --- |
| **TMDB API key** (free) | Always: finding titles, years and episode lists | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api); paste it in Settings → Movie information |
| **A Torznab indexer** | Mainstream movies and shows | [Prowlarr](https://prowlarr.com) or [Jackett](https://github.com/Jackett/Jackett), set up with the sites you use; add it in Settings → Indexers (URL + API key) |
| Download client | Nothing: the built-in engine works on its own | Optional: qBittorrent, Transmission or Deluge in Settings → Download clients |

Without a Torznab indexer, Fetchbox searches only:

- **Internet Archive** (built in): films whose uploader marked them public domain or
  Creative Commons. Their releases don't state a quality, so add them with the **Any**
  profile.
- **Your own indexer definitions**: Fetchbox's YAML format for sites with a JSON or RSS
  API, added with `POST /api/v1/indexer-definitions`. The format is described in
  `src/fetchbox/providers/indexers/definition.py`. Fetchbox ships no definitions for
  other sites.

### Quality profiles

**1080p** (the default), **2160p**, **720p** and **480p** each take only that
resolution, preferring better sources: Blu-ray over WEB over HDTV. **Any** also takes
releases that don't state a quality. Profiles can be edited through the API
(`/api/v1/quality-profiles`).

Files go to `~/Downloads/Fetchbox/Movies` and `.../TV` (Linux: your XDG Downloads folder).
Set `FETCHBOX_DOWNLOADS_ROOT` to change that.

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
