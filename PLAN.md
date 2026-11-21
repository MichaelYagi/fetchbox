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
- [ ] ~~Grow the corpus from real indexer results~~ moved to Phase 2: needs real indexer
      output (the TV work adds most names anyway); `fetchbox parse` turns cases into fixtures
- [x] TMDB provider (`providers/metadata/tmdb.py`; v3 key or v4 token) + discovery
      (`/discovery/search|movies|series`, TTL cache); runtime config `GET/PATCH
      /system/config` (TMDB key encrypted, `FETCHBOX_TMDB_API_KEY` overrides); movie
      titles + wanted items (`/titles`, `/wanted`); `metadata` health check; profile
      delete returns 409 while titles use it
- [x] Scheduled metadata refresh (`refresh_metadata` schedule, every 12 h per the spec:
      movies not refreshed in 7 days, 200 per run, oldest first; skipped without a key)
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
- [x] YAML definition engine (`providers/indexers/definition.py`, own format documented
      in its docstring): declared hosts only (redirects too), rate limit, form login or
      cookie, ordered requests with `when` conditions, JSON / XML responses, templates
      with filters; `indexer_definition` table, `GET/POST/DELETE /indexer-definitions`.
      Built-in: Internet Archive (public-domain films, web-seeded torrents), added as an
      enabled indexer on first run (`FETCHBOX_SEED_INDEXERS`)
- [x] Direct downloads (`providers/download_clients/direct.py`, seeded like the engine):
      one HTTP(S) file, `.part` + Range resume, survives restarts; dropped once imported.
      Definitions with `download: webseed` fetch a torrent's largest video file from its web
      seed: Internet Archive item torrents often can't complete (files change after the
      torrent is made; 4 of 6 popular items stalled at 95-99% in a live test)
- [ ] ~~HTML (CSS selector) responses for definitions~~ moved to Phase 3: no built-in
      definition needs it; it adds a parser dependency
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
- [x] Failure retry: a failed download makes its items due at once, so the next sweep
      grabs the next-best release (the failed one is blocklisted); after
      `search.retry_alternatives` (3) failures in a row the item waits for its interval
- [x] External clients (`providers/download_clients/external.py`): qBittorrent (Web API
      v2, 4.x and 5.x), Transmission (RPC, session id), Deluge (deluge-web JSON-RPC,
      connects to its first daemon); `POST/DELETE /download-clients`,
      `GET /download-clients/kinds`; per-client `policy.path_mappings` (Fetchbox path ->
      client path) for Docker / remote clients
- [x] Seeding goals: client policy `seed_ratio` / `seed_minutes` (whichever first; none
      by default = seed forever) and `seed_action` (`remove` with data, or `pause`); only
      after the import succeeded, never deleting data a symlink import points at;
      `download.seeded_at`, `download.seeded` event
- [x] Local node + default `~/Downloads/Fetchbox/Movies` and `TV` destinations; two-phase
      file ops (`*.fetchbox-partial`, verify size / quick hash, atomic rename; partials
      rolled back at startup); link modes; recycle folder; destination checks (exists,
      write/delete probe, hardlink from the torrent folder, free-space floor) with
      online/offline/low_space events; `/storage/nodes`, `/destinations` (+ check)
- [x] Naming templates (`{Token}`, `{N:00}`, `[optional]`, `{{ }}`), Windows-safe
      components, `/naming-templates` + preview; default Plex/Jellyfin template seeded
- [x] Import (`library.import`, swept on `download.completed`): video pick (no samples),
      on-disk size check, destination check, placement, `media_file`, items `acquired` +
      locked + `cutoff_met`, upgrade replaces into recycle, history; `needs_attention`
      with backoff retry; `/imports` (+ retry), `/files`, `/titles/{id}/files`
- [x] Automatic search (`modules/automation/search.py`): an item is due when
      `next_search_at` is NULL or past; a sweep (every minute, and on `title.added` /
      `download.failed` / `source.added`) queues one `search.wanted` job per due item
      (deduped), which grabs the top pick. Search on add (`search: true`, default),
      missing every 6 h, upgrades (acquired, below cutoff, upgrades allowed, file present)
      every 24 h; `POST /wanted/{id}/search`, `POST /titles/{id}/search` (202 + job).
      Upgrade search judges results against the current file (interactive too)
- [x] Redownload / unlock (`modules/automation/unlock.py`): `POST /wanted/{id}/unlock`
      (clear the lock, optional delete to recycle, `unlocked` history, search: with the
      file kept only a real upgrade is grabbed) and `POST /wanted/{id}/redownload`
      (blocklist the file's release and recycle the file, both default on; the item is
      `missing`, so any acceptable release is grabbed and replaces a kept file)
- [x] mediainfo (`modules/library/probe.py`): pure-Python Matroska/WebM and MP4/MOV
      header reader (no ffprobe/MediaInfo to install): size, resolution class, video
      codec, HDR10 / HLG / Dolby Vision, duration, audio codecs, channels, languages.
      Stored on `media_file.mediainfo`, shown on `/files`; fills naming tokens the release
      name leaves out (the name wins when both say something)
- [x] Minimal UI (`web/`, SvelteKit SPA built into `fetchbox/static`, served with
      deep-link fallback): first-run setup / sign-in, library grid, add (TMDB search,
      quality, search on add), title page (search now, choose a release with rejection
      reasons, monitor, quality, unlock / redownload, files with mediainfo, history),
      queue (progress, pause / resume / remove, imports needing attention), settings
      (health, TMDB key, indexers incl. definitions, download clients, library folders).
      Types generated from the OpenAPI document (`web/openapi.json`, checked in CI)

**Gate:** on a clean machine with no external tools, adding a movie results in a correctly
named file in Downloads.
**Status:** met. `tests/test_direct_downloads.py::test_internet_archive_end_to_end` runs the
built-in Internet Archive definition against a fake archive.org: add a movie (profile
"Any") -> search -> web-seed download -> `Downloads/Fetchbox/Movies/Night of the Living
Dead (1968) {tmdb-10331}/Night of the Living Dead (1968).mp4`. Live, against the real
archive.org, a licensed 34 MB film downloaded in 4 s and was read by the probe. With a
Torznab indexer the same flow is `test_library_api.py::test_gate_add_movie_get_named_file`
(real torrent, real engine). The only setup is a TMDB key; that covers Internet Archive
films. Mainstream releases also need a Torznab indexer (Prowlarr / Jackett): see README.

### Phase 2 — TV + automation
Series/seasons/episodes, six monitor modes; parser TV cases (packs, multi-ep, daily, anime
absolute, specials; grow the parser corpus from real indexer results); optional TVDB;
season-pack rule; RSS sync, missing search, upgrades
(replace only after new import succeeds); calendar.

- [x] Series: add from TMDB with seasons + episodes, one wanted item per episode; six
      monitor modes (`modules/media/series.py`: all, future, missing, first_season,
      latest_season, none; specials never by mode); changing the mode re-applies it;
      refresh (manual and the 12 h schedule) adds new episodes per the mode, and a new
      season takes over under latest_season; `GET /titles/{id}/episodes`,
      `PATCH /titles/{id}/seasons/{n}`; series counts on titles; episode fields on /wanted
- [x] TV search: episode and season queries (tvsearch season/ep, TVDB/IMDb ids, daily shows
      by air date); interactive `GET /releases?wanted_item_id=` (episode) and
      `?title_id=&season=` (season: packs and singles; a grabbed pack claims every episode
      it covers); automatic search sweeps due aired episodes into one job per season
      (season search, packs first then gap filling, then up to 5 episode searches for
      what's left); `POST /titles/{id}/search` queues every season with aired episodes to
      get; unaired episodes are never searched
- [x] TV import: every video file (no samples) matched to its episodes by its own name
      (S/E, 1x04, bare S01E05, daily air dates, anime absolute numbers), or the release's
      for a one-file download; multi-episode files serve every episode they hold
      (EpisodeEnd); wanted episodes a pack lacks go back to missing and are searched
      again; series / season / episode naming. Upgrades replace after the new file is in
      place; a multi-episode file still serving other episodes is kept for them. Default
      episode pattern brackets the quality (`[WEBDL-1080p]`); installs on the old default
      are updated
- [x] Prowlarr indexer kind (`providers/indexers/prowlarr.py`): Prowlarr's address + API
      key; its enabled torrent indexers (listed from `/api/v1/indexer`, every 10 min) are
      searched together through their Torznab feeds; one failing doesn't fail the search.
      Checked against a real Prowlarr 3.0.2. Pasted API keys are trimmed; a Torznab URL that
      is an app's own API (Prowlarr's main address) gets an error saying so
- [x] RSS sync (every 15 min, schedule `rss_sync`): every indexer's feed (Prowlarr: each
      of its indexers; checked against real Prowlarr 3.0.2), releases matched to library
      titles by name (year for movies), the wanted items they'd cover (monitored, aired /
      available, not busy) decided and grabbed exactly as a search would (grab reason
      `rss`); an item's next search is left alone; one broken feed doesn't stop the rest
- [x] **Library settings UI:** Settings > Library has a Movies card and a TV card: the
      folder (status, free space, change), naming style presets (Plex / Jellyfin / Emby,
      Kodi, Simple, Custom), TV season folders (Season 01 / Season 1 / S01 / none), custom
      patterns with click-to-insert tokens, a live preview tree with edge cases (multi-
      episode, specials, no year, characters Windows forbids), and "rename existing
      files" with a review list first (same-disk rename, emptied folders removed, an
      existing target is never overwritten). Movie and TV naming are stored separately.
      API: `/library/naming-options`, `/library/naming/{kind}` (+ `/preview`),
      `/library/rename/{kind}`
- [x] UI: add page with Movies / TV shows (monitor mode for shows, "start searching" for
      movies); series page with seasons (monitor, search, choose a release incl. packs) and
      episodes (monitor, choose a release); library tiles show episodes downloaded
- [x] Movies: minimum availability (announced / in cinemas / released) from TMDB release
      dates; automatic search waits until then
- [ ] Calendar (`GET /calendar?start=&end=`) + calendar page
- [ ] Optional TVDB (scene / absolute numbering)
- [ ] Grow the parser corpus from real indexer results

**Gate:** a "future only" series grabs a new episode via RSS; season-pack selection correct;
an upgrade replaces the file safely.

### Phase 3 — Ecosystem + existing libraries
qBittorrent/Transmission/Deluge; path mappings + client path probe; mount nodes, 60 s health
checks, all link modes, recycle folder, duplicate detection; existing-library import
(read-only until apply), match correction, rename preview, maintenance; Plex/Jellyfin/Emby;
webhook + Discord; interface bind + kill switch; HTML (CSS selector) responses for
indexer definitions.

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
- Naming: `[ ]` marks an optional segment and is not printed (per the spec's episode
  example), so the default movie file is `Title (Year) - Quality HDR Codec-Group`;
  `[[ ]]` gives literal brackets. Names are made Windows-safe on every OS.
- Link mode `move` links or copies at import (so seeding keeps working); the torrent's
  own copy goes when a seeding goal removes it with its data.
- Deleting a file (`DELETE /files/{id}`) keeps the item locked; getting it again is an
  explicit redownload.
- Titles compare without spaces after normalising (`d'Amélie` = `d.Amelie`).
- Decision engine details the spec leaves open: a PROPER/REPACK of the same tier ranks
  above format score (and counts as an upgrade); seeders rank by order of magnitude so
  60 vs 40 doesn't override indexer priority; release year may differ by 1; samples are
  rejected; the same info hash from several indexers is listed once (best priority).
  Extra reasons: `wrong_media_type`, `season_not_finished`, `season_pack_not_wanted`,
  `upgrades_disabled`, `cutoff_met`, `not_an_upgrade`.
- Automatic search settings in `search`: `automatic` (scheduled searches on/off; off
  still searches new titles and failed downloads), `missing_every_hours` (6),
  `upgrade_every_hours` (24), `retry_alternatives` (3). Per-item cadence is
  `next_search_at`, not a schedule row: one row per item would be noise in
  `/automation/schedules`. No search runs while no indexer is enabled; items stay due.
- The `searching` state is held only during an automatic search of a missing item;
  a crash leaves it to startup recovery (back to `missing`, due).
- Unlock / redownload respond 200 with the item and `search_job_id` (spec leaves the
  response open); events `wanted.unlocked` / `wanted.redownload`. "Already grabbed" and
  grab de-duplication only count grabs since the item's last unlock, so a redownload
  without blocklisting can take the same release again; if that torrent is still
  seeding, its completed download is reused (rechecked, imported again).
- A failed upgrade download returns its item to `acquired` (it still has its file),
  not `missing`. Removing a download by hand doesn't trigger a new search.

- Definitions use fixed setting slots (`base_url`, `api_key`, `username`, `password`,
  `cookie`) plus declared non-secret `options`, so secrets are always encrypted at rest.
  `DELETE /indexer-definitions/{slug}` (user definitions not in use) is an addition.
- `GET /titles/{id}/episodes` and `PATCH /titles/{id}/seasons/{n}` (the spec lists no
  episode endpoints). A series' `TitleOut.wanted` is empty; `episodes` carries counts.
  An episode with no air date counts as not aired.
- Default profiles are named by resolution and take only that resolution: 1080p (the UI's
  default), 2160p, 720p, 480p; plus "Any" (Unknown, SD, 720p, 1080p; cutoff 1080p) for
  releases that don't state a quality; "Unknown" is left out of file names (the file's
  resolution is used when it can be read).
- `GET /auth/status` (public): whether first-run setup is pending, for the UI's first
  screen. `fetchbox openapi` prints the API document for client generation.
- SvelteKit 3 config lives in `vite.config.ts` and uses `#lib` (package.json imports)
  instead of `$lib`.
- Removing a download client keeps its finished downloads (`client_id` becomes null);
  it is refused while the client has unfinished or still-seeding downloads, and for
  the built-in engine. A finished torrent counts as seeding even when the client has
  stopped it.
- Built-in definitions are limited to sources that distribute content legally (Internet
  Archive so far, and only items whose uploader marked them public domain or Creative
  Commons: the `feature_films` collection also holds uploads that are neither). Definitions for other sites are the user's choice, added through
  `POST /indexer-definitions`.

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
5. Windows/NTFS: hardlinks, UNC paths, illegal characters, WSL path mappings. (Resolved)
   On Windows, libtorrent reports a torrent finished before its memory-mapped writes
   reach the file: reading it then gave short/wrong bytes (and pytest's diff of the
   failed 2 MB assert looked like a hang). A `flush_disk_cache` resume save fixed it 7/8 times
   and `always_pwrite` never; pausing (closes files after pending writes) 8/8. The
   engine now pauses a torrent on finish and reports `seeding` once paused, then
   resumes it. Full suite passes on native Windows. Import should still verify size before linking.
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
9. SQLite "database is locked" under load (writers waiting > busy_timeout 5 s): seen in
   Phase 1 and reproducible in `tests/test_tv_search.py::test_automatic_search_packs_first_then_episodes`
   (job claims, poll updates, API-key touches, activity inserts time out). Instrumenting
   showed no SQLAlchemy connection inside a transaction at the moment of the error, so
   it is not a transaction left open. Effects are retried or best-effort (claims, polls,
   key touches), but up to 5 s stalls are possible. Next: try `BEGIN IMMEDIATE` for
   writing sessions / a dedicated writer connection, and check WAL checkpointing.
