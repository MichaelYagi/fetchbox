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

### Docker

```sh
docker run -d --name fetchbox -p 8787:8787 -p 6881:6881 -p 6881:6881/udp \
  -e FETCHBOX_ADMIN_PASSWORD=change-me -e PUID=1000 -e PGID=1000 \
  -v fetchbox-config:/config -v /srv/media:/data ghcr.io/michaelyagi/fetchbox
```

`/config` holds the database and settings. `/data` is one folder for downloads and the
library (`Torrents/`, `Movies/`, `TV/`), so finished downloads are hardlinked into the
library instead of copied. Set `PUID`/`PGID` to the owner of your media folder. The
password is only used on first start; change it in the web UI. A compose file is in
`docker/compose.yaml`.

### Desktop apps

Each release has a Windows app, a macOS app (Apple Silicon and Intel) and a Linux binary.
Windows and macOS start in the tray / menu bar and open Fetchbox in your browser. They're
unsigned: the bundled README.txt explains getting past SmartScreen or Gatekeeper.
Building them yourself: `packaging/README.md`.

### What you need

| | Needed for | Where to get it |
| --- | --- | --- |
| **TMDB API key** (free) | Always: finding titles, years and episode lists | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api); paste it in Settings → Movie information |
| **Prowlarr** (or Jackett) | Mainstream movies and shows | [Prowlarr](https://prowlarr.com), set up with the sites you use. In Settings → Indexers, add **Prowlarr** with the address you open it at (e.g. `http://192.168.1.10:9696`) and its API key (Prowlarr: Settings → General → Security). Every enabled Prowlarr indexer is searched, including ones added later. For Jackett or a single feed, use **Torznab feed** with that feed's URL |
| Download client | Nothing: the built-in engine works on its own | Optional: qBittorrent, Transmission or Deluge in Settings → Download clients |

Without Prowlarr or Jackett, Fetchbox searches only:

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

### Troubleshooting: "database is locked" or stalls

Start Fetchbox with `FETCHBOX_DB_GUARD=1` (and optionally `FETCHBOX_DB_GUARD_OUT=guard.txt`)
to record what holds the database: write transactions held over a second, a task waiting on
its own write, and any moment the server's event loop is blocked for over half a second,
each with the code responsible. Attach that file to a bug report.

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
