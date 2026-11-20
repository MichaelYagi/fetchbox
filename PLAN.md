# Fetchbox — Delivery Plan

Source of truth for *what* to build: `Media Manager — Engineering Specification.md`.
This file covers *how and in what order*. The spec's embedded roadmap diagram was lost
in the markdown export; the phases below are reconstructed from the rest of the spec.

## Repo layout

```
src/fetchbox/
  app.py  cli.py  config.py
  core/        db, ids, errors, logging, events, providers, health   (no module imports)
  modules/<m>/ router.py service.py repo.py models.py schemas.py      (one per spec module)
  providers/   metadata/ indexer/ download/ storage/ mediaserver/ notifier/
  migrations/  Alembic (inside the package so frozen builds ship it)
  definitions/ built-in indexer YAML
web/           SPA (SvelteKit, static adapter) -> built into src/fetchbox/static
tests/         unit + API tests; tests/fixtures/release_names/ parser corpus
packaging/     Dockerfile, PyInstaller specs, installers, smoke tests
```

Layering (`router -> service -> repo/provider`, `core` imports nothing above it) is
enforced by import-linter in CI.

## Phases and gates

A phase starts only when the previous gate passes.

### Phase 0 — Foundations
- [x] Package skeleton, `fetchbox serve`/`migrate`, config precedence, platform data dirs
- [x] Async SQLAlchemy + SQLite WAL, Alembic baseline, migrations at startup
- [x] RFC 9457 problem details, structlog with central secret redaction
- [x] Event envelope + in-process bus with 1,000-event replay buffer
- [x] Provider protocol + registry (built-ins + `fetchbox.providers` entry points)
- [x] Health registry, `/system/health|status|version`
- [x] CI: ruff, mypy, pytest (3 OSes), import-linter, license check, libtorrent freeze smoke
- [ ] Job runner: `job` table, leases, per-queue worker pools, dedupe, backoff, cancel
- [ ] Auth: admin user, API keys (argon2id, prefix lookup), scopes, sessions + CSRF, first-run bootstrap
- [ ] Route-scope coverage test (every OpenAPI route declares a scope)
- [ ] API conventions: cursor pagination, filter/sort, ETag/If-Match, idempotency store
- [ ] Events over WebSocket + SSE (`/events/ws`, `/events/stream`), activity log writer
- [ ] AES-GCM secrets with `secrets.key`; LAN bind refused until a password exists
- [ ] PostgreSQL CI job (asyncpg)

**Gate:** fresh start creates admin + `default` key; a test job survives `kill -9` and
resumes on restart; its events arrive over WebSocket.

### Phase 1 — Movie pipeline, zero-config
TMDB provider + discovery; movie titles + wanted items; quality profiles (Standard HD, 4K),
custom formats, scoring; release parser with a 500+ name fixture corpus; decision engine +
interactive search; Torznab indexer, then YAML definition engine + first built-in definitions;
built-in libtorrent client, queue manager, limits, stall detection; local node + default
`~/Downloads/Fetchbox/Movies`; two-phase file ops; `hardlink_or_copy`; naming templates;
import, lock, blocklist, failure retry, redownload; minimal UI (search, add, queue, library).

**Gate:** on a clean machine with no external tools, adding a movie results in a correctly
named file in Downloads.

### Phase 2 — TV + automation
Series/seasons/episodes, six monitor modes; parser TV cases (packs, multi-ep, daily, anime
absolute, specials); optional TVDB; season-pack rule; RSS sync, missing search, upgrades
(replace only after new import succeeds); calendar.

**Gate:** a "future only" series grabs a new episode via RSS; season-pack selection correct;
an upgrade replaces the file safely.

### Phase 3 — Ecosystem + existing libraries
qBittorrent/Transmission/Deluge; path mappings + client path probe; mount nodes, 60 s health
checks, all link modes, recycle folder, duplicate detection; existing-library import
(read-only until apply), match correction, rename preview, maintenance; Plex/Jellyfin/Emby;
webhook + Discord; interface bind + kill switch.

**Gate:** Docker + remote qBittorrent + NAS mount + Plex: import an existing library, apply,
Plex refreshes.

### Phase 4 — Operability + release (v1.0)
Backups/restore, config export/import, pre-migration backups, diagnostics zip, updates;
Sonarr/Radarr migration wizard; tray (keyring-stored key); packaging (multi-arch Docker,
PyInstaller, Inno Setup, .app, AppImage/.deb); hardening (CSP, brute-force limit, outbound
restrictions, filesystem-boundary tests).

**Gate:** migrate a real Sonarr+Radarr install and run side by side; installers work on all
three OSes; a backup restores on a fresh machine.

## Spike results (2026-11-20)

- **libtorrent 2.1.1** has PyPI wheels for CPython 3.9–3.13 on Windows (win32/amd64),
  manylinux + musllinux (x86_64, aarch64) and macOS 14+/15+ (arm64, x86_64).
  A PyInstaller one-file build on Linux x86_64 started a session correctly (21 MB).
- **No cp314 wheel** → `requires-python = ">=3.12,<3.14"`.
- **No universal2 wheel, macOS 14 minimum** → ship separate arm64 and x86_64 .app builds
  (or lipo-merge), and the spec's "macOS 13+" becomes **macOS 14+** unless we build
  libtorrent ourselves.
- Windows and macOS freezing are covered by the CI smoke job, not yet verified locally.

## Spec fixes to apply

1. `wanted_item UNIQUE (title_id, episode_id)` doesn't stop duplicate movie items (NULLs are
   distinct). Add a partial unique index `ON wanted_item (title_id) WHERE episode_id IS NULL`.
2. `idempotency_record` keyed on `api_key_id`; UI sessions have no key → key on actor id.
3. `POST /system/shutdown` is used in the shutdown flow but missing from the endpoint table.
4. `integrations` / `notifications` lack `:read` scopes.
5. Lock vs upgrade: define one rule. Proposed: `lock='acquired'` blocks re-grab of a
   *missing* file but not profile-allowed upgrades; `lock='manual'` blocks everything.

## Decisions taken here

- Frontend: SvelteKit (static adapter) + TypeScript client generated from OpenAPI.
- Tooling: uv, ruff, mypy (strict on `core`), pytest, import-linter.
- CLI: stdlib `argparse` (no extra dependency).

## Risks

1. libtorrent freezing on Windows/macOS (CI smoke job watches this).
2. Release-parser accuracy — track corpus pass rate.
3. Built-in indexer definitions: maintenance burden, Cloudflare; Torznab ships first.
4. SQLite write contention between job workers and client polling — load test in Phase 0.
5. Windows/NTFS: hardlinks, UNC paths, illegal characters, WSL path mappings.
