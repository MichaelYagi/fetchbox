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
- [x] Job runner: `job` table, leases, per-queue worker pools, dedupe, backoff, cancel,
      graceful checkpoint, crash recovery (real `kill -9` test), `/jobs` API
- [x] Schedules: rows defined in code, user edits persist, deduped runs, ETag'd PATCH,
      run-now; built-ins `health_check` (1 min) and `housekeeping` (daily). Module
      schedules (RSS, metadata, upgrades, backups) arrive with their modules.
- [ ] Time-of-day schedules (e.g. housekeeping at 03:00); interval-only for now
- [x] Auth: admin user, API keys (argon2id, prefix lookup, verify cache), scopes with
      escalation guard, rotate/revoke/expiry, sessions + CSRF, login rate limit,
      first-run bootstrap, `fetchbox reset-password`, LAN bind refused without password
- [x] Route-scope coverage test (enumerates OpenAPI operations; public routes allowlisted)
- [ ] Trusted proxies (`X-Forwarded-For`) and forward-auth header (Authelia/Authentik)
- [x] API conventions: keyset cursor pagination + `sort`, ETag/If-Match on versioned rows
      (concurrent writes -> 412, never a lost update), Idempotency-Key store (per actor,
      24 h, replay / 422 mismatch / 409 in progress, errors not stored)
- [x] Events over WebSocket + SSE (`/events/ws`, `/events/stream`): topic filters,
      Last-Event-ID replay with gap signal, `auth.*` admin-only, stream closes when its
      credential is revoked, single-use stream tokens, same-origin check for cookie WS
- [x] Activity log: batched writer (lossless shutdown), `/activity`, prune job
- [ ] Activity entries for request IPs behind proxies (needs trusted_proxies)
- [x] AES-GCM secrets with `secrets.key` (0600, AAD-bound per field), SecretStr
      seal/open/mask/merge helpers for provider settings; LAN bind refused without password
- [ ] Optional OS-keychain storage of `secrets.key` on desktop; key rotation
- [ ] PostgreSQL CI job (asyncpg)

**Gate:** fresh start creates admin + `default` key; a test job survives `kill -9` and
resumes on restart; its events arrive over WebSocket.
**Status:** covered end to end by `tests/test_phase0_gate.py` (real server processes).
Remaining unchecked items above are Phase 0 hardening, not gate blockers.

### Phase 1 — Movie pipeline, zero-config
- [x] Release parser (`modules/releases/parser.py`): title/alt titles, year, S/E, multi-ep,
      season packs and ranges, daily, anime absolute + versions, quality (source,
      resolution, remux/BR-DISK), codec, bit depth, HDR, audio + channels + Atmos,
      streaming service, edition, languages/multi/subs/hardsubs, group, revision, flags.
      Corpus: 677 names in `tests/fixtures/releases/*.jsonl`, 8 strict-xfail known gaps;
      `fetchbox parse NAME` prints the parse for turning bad cases into fixtures.
- [ ] Grow the corpus from real indexer results once Torznab lands (corpus is hand-written)
- [x] TMDB provider (`providers/metadata/tmdb.py`; v3 key or v4 token) + discovery
      (`/discovery/search|movies|series`, TTL cache); runtime config `GET/PATCH
      /system/config` (TMDB key encrypted, `FETCHBOX_TMDB_API_KEY` overrides); movie
      titles + wanted items (`/titles`, `/wanted`); `metadata` health check; profile
      delete returns 409 while titles use it
- [ ] Scheduled metadata refresh (daily, titles not refreshed in 7 days)
- [x] Quality profiles (Standard HD, 4K), custom formats, scoring: quality catalog with
      default size bounds; ladder of equal-rank tiers + cutoff; Radarr-compatible format
      matching (required / negate / OR within a type, AND across types); language rule;
      defaults seeded while tables are empty; `POST /quality-profiles/{id}/evaluate`
- [x] Decision engine (`modules/releases/decision.py`, `search.py`): every rejection
      reason recorded in spec order; ranking; season-pack-wins selection with gap filling;
      upgrade rule; fan-out over release sources with per-source timeout and failures
      reported, not raised. Pure: blocklist, grab history and rules are passed in.
- [x] `POST /grabs` (Idempotency-Key; same release + item returns the existing grab;
      items claimed with a conditional UPDATE so concurrent grabs can't both win),
      `GET /grabs[/{id}]`, `GET/POST/DELETE /blocklist`; blocklist and grab history feed
      the decision engine; `history_event` + `GET /history`, `GET /titles/{id}/history`
- [x] Torznab indexer (`providers/indexers/torznab.py`): caps, id-first movie/TV search
      with text fallback, extended attrs (seeders, hash, freeleech...), resolve to magnet
      or .torrent; `/indexers` CRUD + kinds + test + reorder; failure backoff (5 min
      doubling to 24 h, Retry-After honoured); `indexers` health check
- [x] Interactive search `GET /releases?wanted_item_id=` (movies): all enabled indexers,
      ranked decisions with reasons, `picks`, per-indexer failures, `release_id`s cached
      in memory for 1 h
- [ ] YAML definition engine + first built-in definitions
- [x] Built-in libtorrent engine (`providers/download_clients/builtin.py`): magnet and
      .torrent, added paused (no libtorrent auto-management), resume data on metadata /
      finish / every 5 min / shutdown, pad files hidden, isolated mode for tests
- [x] Downloads module: grab -> `downloads.add` job (sweep + wake on `release.grabbed`,
      so no grab is lost), client pick, save under `<downloads root>/Torrents/<category>`;
      poller (2 s) with queue manager (`policy.max_active`, priority then age), stall
      detection (no progress + no peers for `stall_minutes`, fail after `stall_limit`);
      failure removes data, blocklists, returns items to `missing`; `/downloads` (+
      pause/resume/recheck/reorder/pause-all/resume-all/limits), `/download-clients`
      (GET/PATCH/test). Built-in client row seeded on first start.
- [ ] Failure retry: search for an alternative release (up to 3)
- [ ] External clients (qBittorrent, Transmission, Deluge) + `POST/DELETE /download-clients`
- [ ] Seeding goals (ratio / time) once import exists
- [ ] Local node + default `~/Downloads/Fetchbox/Movies`; two-phase file ops;
      `hardlink_or_copy`; naming templates
- [ ] Import, lock, blocklist, failure retry, redownload
- [ ] Minimal UI (search, add, queue, library)

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

## Additions beyond the spec (Phase 0)

- `POST /auth/setup` — first-run admin password, localhost-only, until a password exists.
- `GET /system/ping` — unauthenticated liveness probe for Docker healthchecks.
- `POST /events/token` — single-use 60 s token for `?token=` (the spec's "event stream
  token"); browsers can't set headers on WebSocket/EventSource.
- `POST /system/selftest` — diagnostic job with progress; proves runner + stream on a
  live install and drives the gate test.
- `auth_session` table (UI sessions; only a SHA-256 of the cookie token is stored).
- `integrations:read`, `notifications:read` scopes (spec fix 4).
- First-run `default` key is printed once to stdout, never to the (redacted) logs.

## Additions beyond the spec (Phase 1)

- `GET /qualities` — the quality catalog with default size bounds (MB/min).
- `POST /quality-profiles/{id}/evaluate` — preview tier, formats, score, language and
  size verdicts for release names; backs the profile editor.
- `GET/PATCH/DELETE /custom-formats/{id}` (spec lists only GET/POST). Deleting a format
  removes its score from every profile in the same transaction.
- `quality_profile.sizes` (per-quality size overrides) and `custom_format.default_score`
  columns; the spec's schema lacks them but its entities and Sonarr import need them.
- Remux below 720p is `DVD-Remux`, 720p remux is `Remux-1080p` (no Remux-720p exists).
- `quality_profile.upgrade_min_score_increment` (default 1): the spec's "minimum
  format-score improvement" for same-tier upgrades had no column.
- `GET/PATCH /system/config` sections: `tmdb` (api_key, language, include_adult) and
  `search` (min_seeders, season_pack_share). Unknown sections/settings are 422; a field
  set by environment variable is 409. Events carry changed field names, never values.
- `title.updated` and `wanted.updated` events (spec lists only added/removed/refreshed).
- `POST /titles/{id}/refresh` is synchronous for movies (one TMDB call); series refresh
  will be a job.
- `episode.air_date` is a DATE (TMDB gives dates, not UTC times); spec says
  `air_date_utc TEXT`.
- Movie monitor modes are `all` / `none`; `PATCH /wanted/{id}` keeps a movie's mode in
  step with its one wanted item.
- `GET /indexers/kinds` (provider kinds + settings JSON Schema for the UI form).
  Indexer runtime state (failures, backoff) is written without bumping the ETag.
- Interactive search returns `picks` (what automatic search would grab) and keeps
  results in memory for an hour so a grab can name a `release_id`.
- Grabbing a rejected release needs `allow_rejected: true` (422 `release-rejected`
  otherwise); an expired `release_id` is 410 `release-expired`. `GET /grabs?wanted_item_id=`
  lists an item's grabs. `POST /blocklist` accepts a `release_id` from a search.
- `FETCHBOX_DOWNLOADS_ROOT` (default `~/Downloads/Fetchbox`, `XDG_DOWNLOAD_DIR` on
  Linux) and `FETCHBOX_TORRENT_ISOLATED` (loopback, no DHT/LSD/UPnP; tests use it).
- Download statuses: queued, downloading, paused, completed, failed, removed. A stall
  goes back to `queued` (counted in `stalls`) rather than a separate `stalled` status.
  Download runtime state (progress, ratio) is written without bumping the ETag;
  `live` in the API is the client's last poll (rates, peers, ETA).
- A torrent that can't be added for a permanent reason (bad .torrent, key refused)
  fails at once; transient errors retry 3 times.
- Titles compare without spaces after normalising (`d'Amélie` = `d.Amelie`).
- Decision engine details the spec leaves open: a PROPER/REPACK of the same tier ranks
  above format score (and counts as an upgrade); seeders rank by order of magnitude so
  60 vs 40 doesn't override indexer priority; release year may differ by 1; samples are
  rejected; the same info hash from several indexers is listed once (best priority).
  Extra reasons: `wrong_media_type`, `season_not_finished`, `season_pack_not_wanted`,
  `upgrades_disabled`, `cutoff_met`, `not_an_upgrade`.

## Decisions taken here

- Frontend: SvelteKit (static adapter) + TypeScript client generated from OpenAPI.
- Tooling: uv, ruff, mypy (strict on `core`), pytest, import-linter.
- CLI: stdlib `argparse` (no extra dependency).

## Risks

1. libtorrent freezing on Windows/macOS (CI smoke job watches this).
2. Release-parser accuracy — track corpus pass rate. The corpus is hand-written from
   scene/P2P conventions, so 100% on it is not accuracy on real feeds; measure against
   captured indexer results when Torznab lands. Known gaps are strict xfails.
3. Built-in indexer definitions: maintenance burden, Cloudflare; Torznab ships first.
4. (Resolved) CI's Windows/3.12 job hung on three runs: `test_in_progress_returns_409`
   assumed the first request claimed its idempotency key within 50 ms; on slow runners
   the second call won the claim and waited on the gate forever. The test now waits for
   the first request to be running. CI also runs verbose with a 120 s per-test timeout.
4b. SQLite write contention between job workers and client polling. Phase 0 stress run:
   400 jobs, 8 workers, ~2,000 progress writes, 8.9 s, zero lock errors. Re-test with
   Phase 1's real polling load.
5. Windows/NTFS: hardlinks, UNC paths, illegal characters, WSL path mappings. On
   Windows CI, reading a file libtorrent was *seeding from a pre-existing copy* hung
   (twice, same line; not reproducible on a desktop). Downloaded files read fine.
   The bindings ignore `disk_io_constructor`, so the POSIX backend can't be chosen
   from Python. Watch this when import reads files that are still seeding.
6. Custom-format regexes are user-supplied (`profiles:write` only) and run on every
   search result; Python `re` has no timeout, so a pathological pattern can stall a
   search. Length is capped at 500; consider the `regex` module's timeout if needed.
7. `httpx` (runtime since TMDB) pulls in `certifi`, which is MPL-2.0 — not in the spec's
   MIT/BSD/Apache/ISC/PSF list. Used unmodified, MPL's file-level copyleft imposes nothing
   on Fetchbox; CI's GPL/AGPL check passes. Confirm, or swap to the OS trust store
   (`truststore`, MIT).
8. Anime fansub names often state only a resolution (`[SubsPlease] ... (1080p)`), which
   parses to `Unknown-1080p` and no default profile allows it. Decide in Phase 2 whether
   to treat it as WEB-DL (Sonarr's choice) or ship an anime profile.
