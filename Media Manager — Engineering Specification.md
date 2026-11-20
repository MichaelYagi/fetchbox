# Media Manager — Engineering Specification

Nov 20, 2026 · @Mike

## Overview

One self-hosted application replaces Sonarr and Radarr for movies and TV: discover a title, pick a quality profile, and it downloads and organizes the files. With zero configuration it downloads into the OS Downloads folder through a built-in torrent engine. Plex, NAS shares, external torrent clients and indexer proxies are optional add-ons. **Name: Fetchbox**.

### Principles (non-negotiable)

1. **API-first.** Every feature is an API endpoint. The web UI, the tray and the CLI are API clients and hold no private paths into the core.
2. **Modular.** Metadata, indexers, download clients, storage backends, media servers and notifiers sit behind provider interfaces and are registered as plugins.
3. **Works out of the box.** First run needs no Plex, NAS, Jackett, Prowlarr or external client. The built-in engine and built-in indexer definitions are enough.
4. **Manual and automated are equal.** Every automatic decision (release pick, import match, upgrade) can also be made by hand, and every manual action can be scripted.
5. **Deploy anywhere.** Docker, Linux, Windows, macOS and NAS run the same binary layout and the same config; only the launcher differs.

### Scope

| Area | MVP (v1.0) | Roadmap |
| --- | --- | --- |
| Discovery | TMDB search, movie + series pages, resolution picked at add time | TVDB/Trakt providers, lists import |
| Sources | Built-in indexer definitions, Torznab, add/remove/test/prioritize | Indexer health scoring, per-indexer rate limits UI |
| Releases | Release parser, quality profiles, custom-format scoring, manual pick | Automation rules engine |
| Downloads | Built-in engine, qBittorrent, Transmission, Deluge, queue + limits + seeding policy | Custom client SDK, remote agents |
| Library | Import existing folders, match correction, naming templates, hardlinks | Maintenance reports, stats dashboard |
| Monitoring | RSS sync, missing search, upgrades, do-not-redownload | Calendar view, release notifications |
| Storage | Storage nodes (local + mounted), path mappings, health | Remote storage agents |
| Integrations | Plex/Jellyfin/Emby library refresh | Watched-state sync |
| Notifications | Webhook, Discord | Telegram, email, ntfy |
| System | API keys + scopes, jobs, events, activity log, backups, migrations | Auto-update, config sync |
| Migration | Sonarr/Radarr import | — |

### Gaps added beyond the brief

The brief is strong; these are the items it implies but doesn't spell out, and each one is load-bearing.

- **A built-in torrent engine.** "Works out of the box" with no external client means embedding libtorrent. This is the largest single component.
- **Built-in indexer definitions.** Without Jackett/Prowlarr, Fetchbox needs its own declarative indexer format (YAML, designed from scratch) and its own definitions, plus Torznab as the escape hatch.
- **A release-name parser.** Parsing title, year, season/episode, resolution, source, codec, HDR, audio, group, proper/repack and language from scene names is where Sonarr/Radarr spend most of their complexity. It needs a large fixture-driven test corpus from day one.
- **Custom formats and scoring.** Quality profiles alone can't express "prefer group X, reject cams, prefer Atmos". Profiles combine a quality ladder with scored custom formats.
- **TV edge cases.** Season packs vs single episodes, specials (season 0), daily shows, anime absolute numbering, multi-episode files and alternate/scene titles.
- **Blocklist.** Releases that failed or were rejected never get grabbed again, separate from the do-not-redownload flag on media.
- **Monitoring modes.** Per series: all, future only, missing only, first season, latest season, none.
- **Network binding / VPN guard.** Bind the engine to an interface and pause all torrents if it disappears (kill switch).
- **UI login.** API keys serve machines; people need a username/password (or SSO/forward-auth) session for the web UI.
- **Per-language preferences.** Original vs dubbed audio and subtitle requirements in profiles.
- **Subtitles** (roadmap): a provider slot so a Bazarr equivalent can plug in later.

## Architecture and module boundaries

Fetchbox is a modular monolith: one process, one database, strict module boundaries enforced in code. Splitting into services buys nothing for a self-hosted single-user app and costs a lot in deployment; the provider interfaces and the event bus give the same replaceability. Remote storage agents (roadmap) are the only out-of-process component.

&#91;embedded content: system architecture · clients, API, core modules, providers\]

The UI, tray and scripts are peers behind one API; only providers touch indexers, clients, disks and media servers.

### Tech stack

| Layer | Choice | Why |
| --- | --- | --- |
| Language / API | Python 3.12, FastAPI, Pydantic v2 | OpenAPI generated from code, async I/O, fits existing Argus/Kaeru tooling |
| Persistence | SQLAlchemy 2 (async) + Alembic; SQLite in WAL mode by default, PostgreSQL optional | Zero-config default, real DB for big libraries |
| Torrent engine | libtorrent 2.x (rasterbar) Python bindings | Mature, magnet + DHT + PEX, per-torrent limits, resume data |
| Jobs | In-process job runner backed by the `job` table | Survives restarts without Redis or Celery |
| Events | In-process async event bus, fanned out over WebSocket and SSE | One stream for UI, webhooks and the activity log |
| Web UI | SPA built to static files, served by the API process | UI is just another API client |
| Tray | pystray launcher process | Starts/stops the server, opens the UI |
| Packaging | Docker image (linux/amd64, arm64); PyInstaller builds for Windows, macOS, Linux | Same core everywhere |

**Licensing.** Fetchbox is MIT and contains no code or definitions taken from Sonarr, Radarr, Prowlarr or Jackett. Fetchbox can still talk to them over their public APIs (Torznab, the migration importer), because that is interoperability, not reuse. Dependencies must be MIT-compatible (MIT, BSD, Apache-2.0, ISC, PSF); libtorrent is BSD-3-Clause. pystray is LGPL-3.0: an MIT app may use it as an unmodified library, but a frozen build has to let users swap it. If that's unwanted, replace it with a small per-OS tray shim. CI runs a license check (`pip-licenses` plus the frontend equivalent) and fails on GPL or AGPL packages.

### Layering

Requests flow `API router → service → repository / provider`. Routers hold no logic. Services own their module's tables; another module reads them only through that module's service interface, never by joining its tables. Providers are the only code that talks to the outside world (HTTP, filesystem, torrent engine).

### Modules

| Module | Owns | Depends on |
| --- | --- | --- |
| `discovery` | Metadata lookup, title cache, artwork | metadata providers |
| `media` | Movies, series, seasons, episodes, monitoring state | discovery |
| `sources` | Indexers, definitions, health, rate limits | indexer providers |
| `releases` | Release search, parsing, scoring, decision engine, blocklist | sources, profiles, media |
| `downloads` | Download clients, queue, policies, seeding | download-client providers, storage |
| `library` | Media files, import, matching, renaming, maintenance | media, storage, profiles |
| `storage` | Storage nodes, roots, path mappings, health | storage providers |
| `profiles` | Quality profiles, custom formats, naming templates | — |
| `automation` | RSS sync, scheduled searches, upgrade checks, rules | releases, downloads, media |
| `integrations` | Plex / Jellyfin / Emby connections | media-server providers |
| `notifications` | Notification targets and routing | notifier providers, events |
| `auth` | Users, API keys, scopes, sessions | — |
| `system` | Jobs, events, activity log, health, backups, updates, config | everything (read-only) |

A dependency arrow never points back up this table except through events. For example, `downloads` emits `download.completed`; `library` subscribes and imports. `downloads` never calls `library`.

## Domain model

The core abstraction is the **wanted item**: the smallest unit Fetchbox acquires. A movie is one wanted item; a series contributes one per episode. Monitoring, searching, grabbing, importing, upgrading and do-not-redownload all operate on wanted items, so movies and TV share one pipeline instead of two apps' worth of code.

### Entities

| Entity | What it is | Key fields |
| --- | --- | --- |
| `Title` | A movie or a series (single-table, `kind` discriminator) | kind, tmdb\_id, tvdb\_id, imdb\_id, name, year, status, profile\_id, destination\_id, monitor\_mode, tags |
| `Season` | A season of a series | title\_id, number, monitored |
| `Episode` | One episode | season\_id, number, absolute\_number, air\_date\_utc, name |
| `WantedItem` | Acquirable unit: one movie or one episode | title\_id, episode\_id?, state, monitored, lock, last\_search\_at, cutoff\_met |
| `MediaFile` | A file on disk linked to wanted item(s) | storage\_node\_id, relative\_path, size, hash?, quality, custom\_format\_score, mediainfo, imported\_at |
| `Release` | One search result from an indexer (ephemeral, cached) | indexer\_id, guid, title, size, seeders, info\_hash, magnet/url, parsed fields, published\_at |
| `Grab` | A decision to download a release for wanted item(s) | release snapshot, wanted\_item\_ids, reason (manual / rss / search / upgrade), score, idempotency\_key |
| `Download` | The client-side job for a grab | grab\_id, client\_id, client\_ref (info hash), status, progress, save\_path, ratio, seed\_time |
| `Indexer` | A configured torrent source | definition\_id, name, enabled, priority, settings (encrypted), categories, tags |
| `IndexerDefinition` | Declarative indexer spec (built-in or user) | slug, version, yaml |
| `DownloadClient` | Built-in engine or an external client | kind, name, settings, priority, categories, tags, enabled |
| `StorageNode` | A place files live | kind (local / mount / agent), name, status, capacity |
| `Destination` | A root folder on a node, for movies or TV | node\_id, path, media\_kind, naming\_template\_id, link\_mode, free\_space\_min |
| `PathMapping` | Same location seen differently by two parties | client\_id or node\_id, remote\_prefix, local\_prefix |
| `QualityProfile` | Allowed qualities, order, cutoff, upgrades | ladder, min/max, cutoff, upgrade\_allowed, min\_format\_score, cutoff\_format\_score, languages |
| `CustomFormat` | Named matcher + score (codec, HDR, group, audio, regex) | conditions, default\_score |
| `NamingTemplate` | Folder and file name patterns | movie\_folder, movie\_file, series\_folder, season\_folder, episode\_file |
| `BlocklistEntry` | A release never to grab again | info\_hash / guid, title\_id, reason |
| `HistoryEvent` | Immutable record per wanted item | type (grabbed, imported, failed, deleted, upgraded, unlocked), data |
| `Job` | Background unit of work | type, status, payload, progress, attempts, parent\_id |
| `ActivityEntry` | System-wide audit line | category, action, actor, subject, outcome |
| `User`, `ApiKey` | Identities | key prefix, hash, scopes, expires\_at, last\_used\_at |
| `Integration`, `NotificationTarget` | Outbound connections | kind, settings, event filters |

### Wanted-item states

`missing → searching → grabbed → downloading → importing → acquired`. Failures route back to `missing` and blocklist the release. An item reaches `acquired` with `cutoff_met` true or false; only items with `cutoff_met = false` and `upgrade_allowed` stay eligible for automatic upgrades.

### Do-not-redownload

On first successful import, the wanted item gets `lock = acquired`. Automation ignores locked items, even if the file is later deleted or a better release appears (unless the profile allows upgrades, which the user chose explicitly). Unlocking is one action (`POST /wanted/{id}/unlock`, or the "Redownload" button) that clears the lock, writes an `unlocked` history event, optionally deletes the current file, and queues a search. A locked item whose file disappears is surfaced in Library maintenance as "locked but missing" rather than silently re-grabbed.

## Provider interfaces

Every external dependency is a provider: a class implementing one protocol, registered under a `kind` string, configured by a Pydantic settings model that also generates its UI form. Built-in providers ship in-tree; third-party ones load from Python entry points (`fetchbox.providers`). Every provider exposes `test()` and `health()` so the UI and the health monitor treat them uniformly.

```python
class Provider(Protocol):
    kind: ClassVar[str]                # "qbittorrent", "tmdb", "plex"
    Settings: ClassVar[type[BaseModel]]  # drives config validation + UI form
    capabilities: ClassVar[set[str]]
    async def test(self) -> TestResult: ...
    async def health(self) -> HealthStatus: ...
```

### Metadata

```python
class MetadataProvider(Provider):
    async def search(self, query: str, kind: MediaKind, year: int | None) -> list[TitleSummary]: ...
    async def get_movie(self, ext_id: str) -> MovieDetail: ...
    async def get_series(self, ext_id: str) -> SeriesDetail: ...       # seasons + episodes
    async def get_changes(self, since: datetime) -> list[str]: ...      # incremental refresh
    async def find_by_external_id(self, source: str, value: str) -> TitleSummary | None: ...
```

Default: TMDB for both movies and TV (requires the user's free TMDB key, or a bundled proxy key; see open decisions). TVDB as an optional second source for scene/absolute numbering.

### Indexer

```python
class IndexerProvider(Provider):
    async def search(self, q: SearchQuery) -> list[ReleaseCandidate]: ...  # by text, ids, season/episode
    async def rss(self) -> list[ReleaseCandidate]: ...                      # latest feed
    async def resolve(self, c: ReleaseCandidate) -> TorrentSource: ...      # magnet or .torrent bytes
```

Implementations: `definition` (runs a YAML indexer definition: login, search URL templates, HTML/JSON selectors, category maps), `torznab` (any Jackett/Prowlarr/native Torznab endpoint), `rss` (plain feed). Capabilities declare `search.imdb`, `search.tvdb`, `search.season`, `rss`.

### Download client

```python
class DownloadClient(Provider):
    async def add(self, src: TorrentSource, opts: AddOptions) -> ClientRef: ...  # save_path, category, paused, limits
    async def status(self, refs: list[ClientRef] | None = None) -> list[ClientTorrent]: ...
    async def files(self, ref: ClientRef) -> list[ClientFile]: ...
    async def set_limits(self, ref: ClientRef | None, limits: TransferLimits) -> None: ...  # None = global
    async def set_priority(self, ref: ClientRef, prio: int) -> None: ...
    async def pause(self, ref: ClientRef) -> None: ...
    async def resume(self, ref: ClientRef) -> None: ...
    async def remove(self, ref: ClientRef, delete_data: bool) -> None: ...
    async def recheck(self, ref: ClientRef) -> None: ...                     # verification
```

Implementations: `builtin` (libtorrent), `qbittorrent` (WebUI API v2), `transmission` (RPC), `deluge` (Web JSON-RPC). Fetchbox polls `status()` every few seconds; clients that support push may emit instead. Queue rules (max active, priorities) are enforced by Fetchbox, not the client, so behaviour is identical across clients.

### Storage

```python
class StorageBackend(Provider):
    async def stat(self, path: str) -> FileStat | None: ...
    async def list(self, path: str, recursive: bool = False) -> AsyncIterator[FileStat]: ...
    async def usage(self, path: str) -> DiskUsage: ...
    async def check_access(self, path: str) -> AccessReport: ...   # read, write, hardlink-capable, same-fs-as
    async def move(self, src: str, dst: str) -> None: ...
    async def copy(self, src: str, dst: str, verify: bool = True) -> None: ...
    async def hardlink(self, src: str, dst: str) -> None: ...
    async def symlink(self, src: str, dst: str) -> None: ...
    async def delete(self, path: str, to_recycle: bool = True) -> None: ...
```

Implementations: `local` (includes OS-mounted NAS shares), `agent` (roadmap: talks to a remote Fetchbox agent over HTTPS).

### Media server

```python
class MediaServer(Provider):
    async def libraries(self) -> list[ServerLibrary]: ...
    async def refresh(self, path: str | None = None) -> None: ...   # partial scan when supported
```

Implementations: `plex`, `jellyfin`, `emby`.

### Notifier

```python
class Notifier(Provider):
    async def send(self, n: Notification) -> None: ...   # title, body, event type, image, links
```

Implementations: `webhook`, `discord` in MVP; `telegram`, `email` (SMTP), `ntfy` later. Routing (which events reach which target) lives in the notifications module, not in providers.

## Database schema

The schema is portable between SQLite and PostgreSQL: TEXT ULID primary keys (sortable, safe to expose, merge-friendly for config import), UTC timestamps as ISO-8601 text on SQLite and `timestamptz` on Postgres, JSON columns for provider settings and parsed release data. Secrets inside settings are encrypted with a per-install key stored outside the database. Every schema change is an Alembic migration; no `create_all` outside tests.

```sql
-- media
CREATE TABLE title (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('movie','series')),
  tmdb_id INTEGER, tvdb_id INTEGER, imdb_id TEXT,
  name TEXT NOT NULL, sort_name TEXT, year INTEGER, status TEXT,
  profile_id TEXT NOT NULL REFERENCES quality_profile(id),
  destination_id TEXT REFERENCES destination(id),
  monitor_mode TEXT NOT NULL DEFAULT 'all',
  metadata JSON, added_at TEXT NOT NULL, refreshed_at TEXT,
  UNIQUE (kind, tmdb_id)
);
CREATE TABLE season  (id TEXT PRIMARY KEY, title_id TEXT NOT NULL REFERENCES title(id) ON DELETE CASCADE,
  number INTEGER NOT NULL, monitored BOOLEAN NOT NULL DEFAULT 1, UNIQUE (title_id, number));
CREATE TABLE episode (id TEXT PRIMARY KEY, season_id TEXT NOT NULL REFERENCES season(id) ON DELETE CASCADE,
  number INTEGER NOT NULL, absolute_number INTEGER, air_date_utc TEXT, name TEXT, UNIQUE (season_id, number));

CREATE TABLE wanted_item (
  id TEXT PRIMARY KEY, title_id TEXT NOT NULL REFERENCES title(id) ON DELETE CASCADE,
  episode_id TEXT REFERENCES episode(id) ON DELETE CASCADE,
  state TEXT NOT NULL DEFAULT 'missing', monitored BOOLEAN NOT NULL DEFAULT 1,
  lock TEXT,                       -- NULL | 'acquired' | 'manual'
  cutoff_met BOOLEAN NOT NULL DEFAULT 0,
  last_search_at TEXT, next_search_at TEXT,
  UNIQUE (title_id, episode_id)
);
CREATE INDEX ix_wanted_search ON wanted_item (monitored, lock, state, next_search_at);

-- library
CREATE TABLE media_file (
  id TEXT PRIMARY KEY, storage_node_id TEXT NOT NULL REFERENCES storage_node(id),
  destination_id TEXT REFERENCES destination(id), relative_path TEXT NOT NULL,
  size INTEGER NOT NULL, mtime TEXT, quick_hash TEXT,      -- first+last 4 MiB, for duplicate detection
  quality JSON NOT NULL, custom_format_score INTEGER NOT NULL DEFAULT 0,
  mediainfo JSON, release_group TEXT, original_name TEXT, imported_at TEXT NOT NULL,
  UNIQUE (storage_node_id, relative_path)
);
CREATE TABLE media_file_item (media_file_id TEXT REFERENCES media_file(id) ON DELETE CASCADE,
  wanted_item_id TEXT REFERENCES wanted_item(id) ON DELETE CASCADE,
  PRIMARY KEY (media_file_id, wanted_item_id));   -- multi-episode files

-- releases & downloads
CREATE TABLE grab (
  id TEXT PRIMARY KEY, release JSON NOT NULL, indexer_id TEXT REFERENCES indexer(id),
  info_hash TEXT, reason TEXT NOT NULL, score INTEGER, actor TEXT NOT NULL,
  idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE grab_item (grab_id TEXT REFERENCES grab(id) ON DELETE CASCADE,
  wanted_item_id TEXT REFERENCES wanted_item(id) ON DELETE CASCADE, PRIMARY KEY (grab_id, wanted_item_id));
CREATE TABLE download (
  id TEXT PRIMARY KEY, grab_id TEXT NOT NULL REFERENCES grab(id),
  client_id TEXT NOT NULL REFERENCES download_client(id), client_ref TEXT NOT NULL,
  status TEXT NOT NULL, queue_priority INTEGER NOT NULL DEFAULT 0, progress REAL NOT NULL DEFAULT 0,
  save_path TEXT, ratio REAL, seed_seconds INTEGER, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  started_at TEXT, completed_at TEXT, removed_at TEXT, UNIQUE (client_id, client_ref)
);
CREATE TABLE blocklist (id TEXT PRIMARY KEY, title_id TEXT REFERENCES title(id) ON DELETE CASCADE,
  info_hash TEXT, guid TEXT, release_title TEXT, reason TEXT, created_at TEXT NOT NULL);

-- configuration
CREATE TABLE indexer_definition (id TEXT PRIMARY KEY, slug TEXT UNIQUE, version TEXT, source TEXT, yaml TEXT NOT NULL);
CREATE TABLE indexer (id TEXT PRIMARY KEY, definition_id TEXT REFERENCES indexer_definition(id), kind TEXT NOT NULL,
  name TEXT NOT NULL UNIQUE, enabled BOOLEAN NOT NULL DEFAULT 1, priority INTEGER NOT NULL DEFAULT 25,
  settings JSON NOT NULL, tags JSON, disabled_until TEXT, failure_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE download_client (id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL UNIQUE,
  enabled BOOLEAN NOT NULL DEFAULT 1, priority INTEGER NOT NULL DEFAULT 1, settings JSON NOT NULL,
  media_kinds JSON, tags JSON, policy JSON);    -- policy: max_active, limits, seed ratio/time, removal
CREATE TABLE storage_node (id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL UNIQUE,
  settings JSON NOT NULL, status TEXT NOT NULL DEFAULT 'unknown', last_seen_at TEXT, usage JSON);
CREATE TABLE destination (id TEXT PRIMARY KEY, storage_node_id TEXT NOT NULL REFERENCES storage_node(id),
  name TEXT NOT NULL, path TEXT NOT NULL, media_kind TEXT NOT NULL, naming_template_id TEXT REFERENCES naming_template(id),
  link_mode TEXT NOT NULL DEFAULT 'hardlink_or_copy', free_space_min INTEGER, is_default BOOLEAN NOT NULL DEFAULT 0);
CREATE TABLE path_mapping (id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
  remote_prefix TEXT NOT NULL, local_prefix TEXT NOT NULL);
CREATE TABLE quality_profile (id TEXT PRIMARY KEY, name TEXT UNIQUE, ladder JSON NOT NULL, cutoff TEXT,
  upgrade_allowed BOOLEAN NOT NULL DEFAULT 1, min_format_score INTEGER DEFAULT 0, cutoff_format_score INTEGER,
  format_scores JSON, languages JSON);
CREATE TABLE custom_format (id TEXT PRIMARY KEY, name TEXT UNIQUE, conditions JSON NOT NULL);
CREATE TABLE naming_template (id TEXT PRIMARY KEY, name TEXT UNIQUE, patterns JSON NOT NULL);

-- system
CREATE TABLE job (id TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL, payload JSON,
  result JSON, progress REAL, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  run_after TEXT, locked_by TEXT, locked_until TEXT, parent_id TEXT REFERENCES job(id),
  dedupe_key TEXT, created_at TEXT NOT NULL, finished_at TEXT);
CREATE INDEX ix_job_ready ON job (status, run_after);
CREATE TABLE history_event (id TEXT PRIMARY KEY, wanted_item_id TEXT, title_id TEXT, type TEXT NOT NULL,
  data JSON, created_at TEXT NOT NULL);
CREATE TABLE activity (id TEXT PRIMARY KEY, category TEXT NOT NULL, action TEXT NOT NULL, actor TEXT,
  subject_type TEXT, subject_id TEXT, outcome TEXT, message TEXT, data JSON, created_at TEXT NOT NULL);
CREATE TABLE app_user (id TEXT PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT, created_at TEXT NOT NULL);
CREATE TABLE api_key (id TEXT PRIMARY KEY, name TEXT NOT NULL, prefix TEXT NOT NULL UNIQUE, key_hash TEXT NOT NULL,
  scopes JSON NOT NULL, created_by TEXT, created_at TEXT NOT NULL, expires_at TEXT, last_used_at TEXT, revoked_at TEXT);
CREATE TABLE idempotency_record (key TEXT, api_key_id TEXT, request_hash TEXT NOT NULL,
  response JSON, status INTEGER, created_at TEXT NOT NULL, PRIMARY KEY (api_key_id, key));
CREATE TABLE integration (id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT UNIQUE, settings JSON NOT NULL, enabled BOOLEAN NOT NULL DEFAULT 1);
CREATE TABLE notification_target (id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT UNIQUE, settings JSON NOT NULL,
  events JSON NOT NULL, enabled BOOLEAN NOT NULL DEFAULT 1);
CREATE TABLE tag (id TEXT PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE title_tag (title_id TEXT REFERENCES title(id) ON DELETE CASCADE, tag_id TEXT REFERENCES tag(id) ON DELETE CASCADE,
  PRIMARY KEY (title_id, tag_id));
```

Retention: `activity` and `history_event` are pruned by a nightly job (defaults 90 days and forever). The release cache lives in memory plus a short-TTL table, not in `grab`; `grab.release` snapshots only what was chosen.

## API contracts

The API is REST + JSON under `/api/v1`, described by a generated OpenAPI 3.1 document served at `/api/v1/openapi.json` with interactive docs at `/api/docs`. The UI uses nothing that isn't in that document; CI fails if a UI call hits an undocumented route.

### Authentication

- **API keys** are sent as `Authorization: Bearer fb_<prefix>_<secret>` (or `X-Api-Key` for \*arr-style clients). Only a hash (argon2id) of the secret is stored; the full key is shown once at creation.
- Keys can be created, named, renamed, given scopes, given an expiry, rotated (new secret, same id and scopes) and revoked or deleted. `last_used_at` and the calling IP are recorded.
- **UI sessions** use username + password with an HttpOnly, SameSite=Strict cookie and CSRF token. Optional: trust a reverse-proxy auth header (Authelia, Authentik) from configured proxy IPs.
- First run creates the admin user and a key named `default` with scope `*`.

### Scopes

Scopes are `resource:action` with actions `read` and `write` (write implies read). The **`*` scope grants everything**, including key management and system operations; it is the "does everything" scope from the brief.

| Scope | Grants |
| --- | --- |
| `*` | All endpoints, present and future |
| `media:read` / `media:write` | Titles, seasons, episodes, wanted items, monitoring, unlock/redownload |
| `discovery:read` | Metadata search and lookup |
| `releases:read` / `releases:write` | Release search, manual grab, blocklist |
| `downloads:read` / `downloads:write` | Queue, pause/resume/remove, priorities, clients |
| `library:read` / `library:write` | Files, import, rename, maintenance |
| `sources:read` / `sources:write` | Indexers and definitions |
| `storage:read` / `storage:write` | Nodes, destinations, path mappings |
| `profiles:read` / `profiles:write` | Quality profiles, custom formats, naming templates |
| `automation:read` / `automation:write` | Schedules, rules, RSS |
| `integrations:write`, `notifications:write` | Outbound connections |
| `events:read` | Event stream subscription |
| `system:read` / `system:write` | Health, logs, jobs, backups, updates, config export/import |
| `auth:admin` | Users and API keys |

### Conventions

- **Pagination:** cursor based, `?limit=50&cursor=…`; responses carry `next_cursor`.
- **Filtering and sorting:** `?filter[state]=missing&sort=-added_at`.
- **Errors:** RFC 9457 problem details (`type`, `title`, `status`, `detail`, `errors[]` for field validation).
- **Concurrency:** resources return an `ETag`; `PATCH`/`PUT`/`DELETE` accept `If-Match` and return 412 on conflict.
- **Long operations** return `202 Accepted` with a `Job` body and a `Location: /api/v1/jobs/{id}` header. Nothing that touches indexers, clients or disk blocks the request.
- **Idempotency:** every `POST` that creates a grab, download, import, search, backup or restore accepts `Idempotency-Key`. Fetchbox stores `(api_key, key) → request hash + response` for 24 hours. A retry with the same key and body returns the original response; the same key with a different body returns 422. Grabs are additionally deduplicated by `(info_hash, wanted_item)`.
- **Versioning:** additive changes stay in v1; breaking changes ship as `/api/v2` alongside v1 for at least one major release.

### Endpoint groups

| Group | Endpoints |
| --- | --- |
| Auth | `GET/POST /auth/keys`, `PATCH/DELETE /auth/keys/{id}`, `POST /auth/keys/{id}/rotate`, `POST /auth/keys/{id}/revoke`, `GET /auth/scopes`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` |
| Discovery | `GET /discovery/search?q=&kind=`, `GET /discovery/movies/{tmdb_id}`, `GET /discovery/series/{tmdb_id}` |
| Titles | `GET/POST /titles`, `GET/PATCH/DELETE /titles/{id}`, `POST /titles/{id}/refresh`, `POST /titles/{id}/search`, `GET /titles/{id}/files`, `GET /titles/{id}/history` |
| Wanted | `GET /wanted?state=missing\|cutoff_unmet`, `PATCH /wanted/{id}` (monitor), `POST /wanted/{id}/search`, `POST /wanted/{id}/unlock`, `POST /wanted/{id}/redownload` |
| Releases | `GET /releases?wanted_item_id=` (interactive search, returns scored + rejected with reasons), `POST /grabs`, `GET /grabs/{id}`, `GET/POST/DELETE /blocklist` |
| Sources | `GET/POST /indexers`, `PATCH/DELETE /indexers/{id}`, `POST /indexers/{id}/test`, `POST /indexers/reorder`, `GET/POST /indexer-definitions` |
| Downloads | `GET /downloads`, `GET/PATCH/DELETE /downloads/{id}`, `POST /downloads/{id}/pause\|resume\|recheck\|retry`, `POST /downloads/reorder`, POST /downloads/pause-all\|resume-all, `GET/POST /download-clients`, `PATCH/DELETE /download-clients/{id}`, `POST /download-clients/{id}/test`, `GET/PUT /downloads/limits` |
| Library | `GET /files`, `PATCH /files/{id}` (rematch), `DELETE /files/{id}`, `POST /library/scan`, `GET /imports`, `POST /imports` (scan a folder), `GET /imports/{id}/candidates`, `POST /imports/{id}/apply`, `POST /library/rename` (preview + apply), `GET /library/maintenance` |
| Storage | `GET/POST /storage/nodes`, `GET/PATCH/DELETE /storage/nodes/{id}`, `GET /storage/nodes/{id}/browse?path=`, `GET/POST /destinations`, `PATCH/DELETE /destinations/{id}`, `POST /destinations/{id}/check`, `GET/POST/DELETE /path-mappings` |
| Profiles | `GET/POST /quality-profiles`, `PATCH/DELETE /quality-profiles/{id}`, `GET/POST /custom-formats`, `GET/POST /naming-templates`, `POST /naming-templates/preview` |
| Automation | `GET/PATCH /automation/schedules`, `POST /automation/schedules/{name}/run`, `GET/POST /automation/rules`, `PATCH/DELETE /automation/rules/{id}` |
| Integrations | `GET/POST /integrations`, `PATCH/DELETE /integrations/{id}`, `POST /integrations/{id}/test` |
| Notifications | `GET/POST /notifications`, `PATCH/DELETE /notifications/{id}`, `POST /notifications/{id}/test` |
| Jobs | `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `POST /jobs/{id}/retry` |
| Events | `GET /events/ws` (WebSocket), `GET /events/stream` (SSE), `GET /activity` |
| History | `GET /history?type=&title_id=` |
| Calendar | `GET /calendar?start=&end=` |
| Stats | `GET /stats` |
| System | `GET /system/health`, `GET /system/status`, `GET /system/version`, `GET /system/diagnostics` (bundle download), `GET /system/logs`, `GET/PATCH /system/config` |
| Backups | `GET/POST /backups`, `GET/DELETE /backups/{id}`, `POST /backups/{id}/restore`, `POST /backups/upload` |
| Config | `GET /config/export`, `POST /config/import` (dry-run by default) |
| Updates | `GET /updates`, `POST /updates/install` |
| Migration | `POST /migrations/arr` (Sonarr/Radarr import, see below) |

### Event envelope

Events share one envelope across WebSocket, SSE, webhooks and the activity log. Clients subscribe with a topic filter such as `download.*`; the server replays from `Last-Event-ID` for up to 1,000 events after a reconnect.

```json
{
  "id": "01JD3Y6M4F8K2Q9R7T5V3W1X0Z",
  "type": "download.completed",
  "occurred_at": "2026-11-20T16:46:00Z",
  "actor": { "type": "api_key", "id": "01JD…", "name": "home-assistant" },
  "subject": { "type": "download", "id": "01JD…" },
  "data": { "title_id": "01JD…", "release": "Show.S01E02.1080p.WEB-DL.x265-GRP", "size": 1450000000 }
}
```

Event types: `title.added|removed|refreshed`, `search.started|completed`, `release.grabbed|rejected`, `download.queued|started|progress|completed|failed|removed`, `import.started|completed|failed`, `file.renamed|deleted|upgraded`, `library.scanned`, `source.added|removed|failed|recovered`, `storage.offline|online|low_space`, `integration.failed`, `job.*`, `system.health_changed`, `auth.key_created|revoked`.

## Core workflows

Each workflow is a chain of jobs linked by events, so any step can be retried alone and every step shows up in the activity log.

### Add a title

1. User searches (`GET /discovery/search`), picks a result and a quality profile; resolution is chosen here by picking the profile (Standard HD, 4K, or custom).
2. Destination defaults to the first destination for that media kind; with none configured it is `~/Downloads/Fetchbox/Movies` or `~/Downloads/Fetchbox/TV`.
3. Fetchbox stores the title, creates wanted items per the monitor mode, and (if "search on add" is ticked) queues a search job for each missing item.

### Search and decide

1. The search job queries all enabled indexers that match the title's tags and capabilities, in parallel, with per-indexer timeouts and rate limits.
2. Each candidate is parsed into structured fields and run through decision specs in order: blocklisted, already grabbed, wrong title/year/episode, size outside quality bounds, below minimum seeders, quality not in profile, format score below minimum, language mismatch, user automation rules.
3. Accepted candidates are ranked by quality ladder position, then custom-format score, then seeders, then indexer priority, then size closeness to preferred.
4. Automatic searches grab the top candidate; interactive search returns the full ranked list, rejected ones included, each with its rejection reasons.
5. For series, a season pack wins when it covers at least the configured share of missing episodes in that season (default: all of them) and the season has finished airing.

### Grab and download

1. `POST /grabs` (idempotent) writes the grab and its wanted-item links, moves items to `grabbed`, and emits `release.grabbed`.
2. The downloads module picks a client: the highest-priority enabled client whose media kinds and tags match, falling back to the built-in engine.
3. The torrent is added with the client-side save path computed from the client's path mapping, and a category label (`fetchbox-movies` / `fetchbox-tv`).
4. Fetchbox's queue manager enforces max active downloads, priorities and speed limits; queued items wait paused in the client.
5. Stalled detection: no progress and no peers for a configurable window (default 2 hours) marks the download `stalled`; after N stalls it fails.

### Import

1. On `download.completed`, an import job resolves the save path back to Fetchbox's view through path mappings.
2. It finds video files (ignoring samples, extras below a size threshold), parses and matches each to wanted items, and reads mediainfo for the real resolution, codec and HDR.
3. It verifies: matched item is still wanted, quality is an upgrade when a file already exists, destination is online, writable and above its free-space floor.
4. It places the file at the naming-template path via the destination's link mode: hardlink when on the same filesystem (so seeding continues), otherwise copy-then-verify, or move once seeding has finished.
5. It writes `media_file`, sets item `acquired` + lock, records history, moves any replaced file to the recycle folder, and asks media-server integrations to refresh that path.
6. Failures (offline destination, no match, permission denied) leave the download in place, mark the import `needs_attention` with the reason, and retry on a backoff.

### Failed downloads

A failed download (client error, stalled limit, verification failure, import rejection the user confirms as bad) blocklists the release, removes it from the client per policy, returns its items to `missing`, and queues a new search when automatic retry is on (default: up to 3 alternative releases).

### Upgrades

A daily job and RSS sync consider items where `cutoff_met = false`, the profile allows upgrades, and the item isn't manually locked. A candidate qualifies only if it is strictly better than the current file on quality ladder or by at least the profile's minimum format-score improvement. The old file is replaced after the new import succeeds, never before.

### Import an existing library

1. User points Fetchbox at a folder (`POST /imports`). Nothing is moved; this is read-only until applied.
2. Fetchbox walks the tree, groups files by folder, and parses folder and file names (including `{tmdb-123}` / `{imdb-tt…}` tags and Plex/Jellyfin naming).
3. It matches each group to a TMDB title with a confidence score; series files are mapped to season/episode, absolute numbers resolved via TVDB when present.
4. Results come back as candidates: confident, ambiguous (several matches) or unmatched, plus duplicates (same item, several files) and missing episodes per series.
5. The user corrects matches in the UI (or via `PATCH`), chooses a profile and destination, then applies. Apply registers files in place, sets items `acquired` + locked, and computes `cutoff_met`.
6. Optional: rename to the naming template, as a separate previewable action.

### Redownload

`POST /wanted/{id}/redownload` = unlock + optional blocklist of the current release + optional delete-to-recycle of the current file + search. The UI offers it on every file and episode row.

## Storage, paths and naming

Storage is its own subsystem because most real-world \*arr failures are path failures. Fetchbox models where files live (storage nodes), where media goes (destinations), and how each party sees a path (path mappings), and checks all three continuously.

### Storage nodes

| Kind | Example | How Fetchbox reaches it | Status values |
| --- | --- | --- | --- |
| `local` | Main server's own disks | Direct filesystem | online, degraded, offline |
| `local` (mount) | NAS share mounted at `/mnt/nas` or `\\nas\media` | Direct filesystem; node also records the expected mount source so an unmounted path (empty dir) is detected | mounted, unmounted, offline |
| `agent` (roadmap) | Desktop, external drive on another machine | Fetchbox agent over HTTPS with a pairing token; agent reports directories, capacity, health | online, on-demand, offline |

On first run Fetchbox creates one `local` node for the host and a default destination in the user's Downloads folder. Detected drives and mounts (via `psutil.disk_partitions`) are offered as suggestions in the UI browser, never added automatically.

### Health checks (every 60 s per destination)

- Path exists and is a directory; for mounts, the mount point is actually mounted (device differs from the parent).
- Read, write and delete a probe file; hardlink capability tested against the download path.
- Free space vs the destination's floor (default 5% or 20 GB); emits `storage.low_space`.
- Transitions emit `storage.offline` / `storage.online`. Imports into an offline destination pause instead of failing; they resume on `storage.online`.

### Path mappings

Fetchbox and each download client may see the same folder under different paths (Docker volumes, a remote qBittorrent, Windows vs WSL). A mapping is a prefix pair scoped to a client or node:

| Scope | Remote prefix (client sees) | Local prefix (Fetchbox sees) |
| --- | --- | --- |
| qBittorrent (Docker) | `/data/downloads` | `/downloads` |
| Transmission on NAS | `/volume1/downloads` | `/mnt/nas/downloads` |
| Windows qBittorrent | `D:\Torrents` | `/mnt/d/Torrents` |

Fetchbox translates outbound (save path it asks the client for) and inbound (completed path the client reports). The client test action runs a path probe: it asks the client for its default save path, translates it, and checks that Fetchbox can see and write there, flagging a mapping mismatch before any real download does.

### Link modes per destination

| Mode | Behaviour | When |
| --- | --- | --- |
| `hardlink_or_copy` (default) | Hardlink if same filesystem, else copy + verify | Keeps seeding without double space |
| `hardlink_only` | Fail import if hardlink impossible | Users who must never double space |
| `copy` | Always copy | Different machines, cautious users |
| `move` | Move after seeding policy completes | No seeding or no space |
| `symlink` | Symlink into the library | Advanced; some media servers dislike it |

### Naming templates

Templates use `{Token}` placeholders with optional formatting and conditional segments in square brackets that vanish when their token is empty. Defaults follow Plex/Jellyfin conventions so libraries scan correctly with no setup.

```text
Movie folder:   {Title} ({Year}) {{tmdb-{TmdbId}}}
Movie file:     {Title} ({Year}) [{Edition}] - {Quality} [{HDR}] [{Codec}]-{Group}
Series folder:  {Title} ({Year}) {{tvdb-{TvdbId}}}
Season folder:  Season {Season:00}
Episode file:   {Title} - S{Season:00}E{Episode:00}[-E{EpisodeEnd:00}] - {EpisodeTitle} [{Quality}]
```

Illegal characters are replaced per target OS of the destination node (Windows rules applied whenever any client or node is Windows or SMB). `POST /naming-templates/preview` renders a template against sample or real titles before saving.

### Filesystem safety

- All moves and copies go through a two-phase operation: write to `<dest>.fetchbox-partial`, verify size (and quick hash on copy), then atomic rename. Interrupted operations are found and resumed or rolled back at startup.
- Deletes go to a per-destination recycle folder with retention (default 7 days) unless disabled.
- Duplicate detection uses (size, quick hash) across all destinations; duplicates are reported, never auto-deleted.

## Jobs, events and reliability

Everything slow runs as a persisted job, and every state change is an event; together they deliver graceful shutdown, automatic recovery and a complete activity log without extra infrastructure.

### Job runner

- Jobs live in the `job` table. Workers claim ready jobs with an atomic update setting `locked_by` and a lease (`locked_until`, default 5 minutes, renewed while running).
- Worker pools per queue keep one slow kind from starving others: `io` (imports, file ops: 2), `net` (searches, indexer RSS: 4), `client` (client polling: 1), `maintenance` (scans, backups: 1).
- `dedupe_key` prevents duplicate pending jobs (for example one `search:wanted:{id}` at a time).
- Retries use exponential backoff with jitter up to `max_attempts`; final failure emits `job.failed` and an activity entry.
- Jobs report `progress` and can be cancelled cooperatively; child jobs (one search per episode in a season search) link to a parent for rollup.
- Schedules (RSS sync every 15 min, refresh metadata every 12 h, upgrade search daily, health checks every minute, backup weekly, housekeeping nightly) are rows the user can edit and trigger by hand.

### Graceful shutdown and recovery

1. On SIGTERM, tray Quit or `POST /system/shutdown`: stop claiming jobs, let running jobs finish up to 30 s, checkpoint the rest (file operations finish their current phase), save libtorrent resume data, close the DB.
2. On startup: release expired leases (those jobs become ready again), scan for `.fetchbox-partial` files and resume or roll back, reconcile downloads against each client (unknown torrents in Fetchbox categories are adopted; missing ones are marked failed), and re-run any import whose download completed while Fetchbox was down.

### Logging and diagnostics

- Structured JSON logs (structlog) with `request_id`, `job_id`, `title_id` correlation fields; human-readable console format when attached to a terminal.
- Rotating files in the data directory (10 × 10 MB default); level configurable per module at runtime.
- Secrets (API keys, passwords, tracker passkeys in URLs) are redacted at the log processor, not at call sites.
- `GET /system/diagnostics` produces a zip: redacted config, last logs, health report, versions, schema revision.

### Health monitoring

One health registry aggregates checks from every provider and subsystem: database, disk space per destination, each indexer, each download client, each integration, metadata provider reachability, update availability. Each check reports `ok | warning | error` with a message and a fix-it link to the relevant settings page. Changes emit `system.health_changed`; the tray icon shows a warning badge when anything is not ok.

### Activity log

The activity module subscribes to all events and writes one human-readable entry per meaningful action, covering every item in the brief (source added/removed, search initiated, release selected, download started/completed, file organized, library scanned, integration failed), filterable by category, title, actor and outcome.

### Backups and restore

- A backup is a zip of a consistent DB snapshot (SQLite online backup API or `pg_dump`), the encrypted-secrets key file (optional, password-wrapped), config file and custom indexer definitions, plus a manifest with app version and schema revision.
- Scheduled weekly, keep last 8; manual on demand; download and upload via API.
- Restore validates the manifest, refuses newer schema revisions than the running app, runs migrations forward for older ones, and restarts the server.

### Migrations and updates

- Alembic migrations run automatically at startup after an automatic pre-migration backup. A failed migration rolls back and the app refuses to start with a clear error (and tray notification) rather than running on a half-migrated schema.
- Updates: the app checks a release feed; desktop builds can download and swap the binary with the tray launcher handling restart; Docker installs show "update available" only.

### Config export / import

`GET /config/export` returns a JSON document of all configuration (profiles, custom formats, naming, indexers, clients, nodes, destinations, mappings, integrations, notifications, schedules, optional secrets) with stable ULIDs. Import diffs it against the current install, shows the plan, and applies it in one transaction.

## Tray launcher and deployment

The server and the tray are separate processes: the server runs headless everywhere, and the tray is a thin desktop launcher that supervises it. This keeps Docker and NAS installs identical to desktop installs, and lets a server crash leave the tray alive to report it.

### Tray menu

| Item | Action |
| --- | --- |
| Open Fetchbox | Opens the web UI in the default browser (uses a one-time login token so no password prompt on localhost) |
| Status line | Health summary and active download count, e.g. "3 downloading · all healthy" |
| Pause all / Resume all | `POST /downloads/pause-all` through the API with the tray's own scoped key |
| Start / Stop / Restart server | Supervises the server process |
| Open logs folder, Open data folder | OS file browser |
| Start at login | Registers with the OS (Registry Run key, LaunchAgent, XDG autostart) |
| Check for updates | `GET /updates`, offers install |
| Settings | Port, bind address (localhost only by default), data directory |
| Quit | Graceful shutdown of the server, then exit |

The tray authenticates with an API key it creates for itself (`tray`, scopes `system:write downloads:write`), stored in the OS keychain via `keyring`.

### Deployment targets

| Target | Package | Notes |
| --- | --- | --- |
| Windows 10/11 | Installer (PyInstaller + Inno Setup) with tray | Data in `%LOCALAPPDATA%\Fetchbox`; Windows paths and UNC shares supported |
| macOS 13+ | Signed .app (universal2) with tray | Data in `~/Library/Application Support/Fetchbox` |
| Linux desktop | AppImage and .deb with tray; systemd user unit optional | Data in `$XDG_DATA_HOME/fetchbox` |
| Linux server | Same binary as a systemd service, no tray | `fetchbox serve --data /var/lib/fetchbox` |
| Docker | `ghcr.io/<owner>/fetchbox` multi-arch image, non-root, `PUID`/`PGID` | Volumes: `/config`, `/data`; documented single-volume layout for hardlinks |
| NAS | Docker image on Synology/QNAP/Unraid/TrueNAS; Unraid community template | Same as Docker |

### Process and config

- One CLI entry point: `fetchbox serve`, `fetchbox tray`, `fetchbox backup`, `fetchbox restore`, `fetchbox migrate`, `fetchbox reset-password`.
- Config precedence: CLI flags → environment variables (`FETCHBOX_PORT`, `FETCHBOX_DATA_DIR`, `FETCHBOX_DB_URL`) → `config.toml` in the data dir → DB settings → defaults.
- Default bind is `127.0.0.1:8787`; binding to the LAN is an explicit setting that forces a password to exist first.
- Recommended Docker layout keeps downloads and library on one volume (`/data/downloads`, `/data/media`) so hardlinks work; the setup wizard warns when downloads and destinations are on different filesystems.

## Security model

Fetchbox holds tracker credentials and can write anywhere it has permission, so it is secure by default: localhost-only until a password exists, least-privilege keys, and secrets never leave the process in plain text.

- **Network exposure:** binds to `127.0.0.1` by default. LAN binding requires a configured admin password. Reverse-proxy deployments set `trusted_proxies` so `X-Forwarded-*` is honoured only from those IPs.
- **Credentials:** passwords hashed with argon2id; API key secrets are 32 random bytes, stored as argon2id hashes, looked up by a non-secret 8-character prefix. Brute-force protection: 10 failed logins per 15 minutes per IP.
- **Secrets at rest:** indexer passwords, cookies, passkeys and client credentials are encrypted with AES-GCM using a key in `secrets.key` (file mode 0600, outside the DB, optionally the OS keychain on desktop).
- **Authorization:** every route declares its required scope; a test enumerates the OpenAPI routes and fails if any route lacks one. Only `*` or `auth:admin` can create keys, and a key can never grant scopes it doesn't hold itself.
- **Filesystem boundary:** writes are only allowed under configured destinations, download paths and the data dir. Paths are normalised and resolved (symlinks included) before the check, blocking `../` escapes from crafted release names.
- **Outbound requests:** indexer definitions can only contact their declared domains; webhook targets can be restricted from reaching private ranges when Fetchbox is internet-exposed.
- **Network guard:** optional bind of the torrent engine to a named interface (e.g. `wg0`) with a kill switch that pauses all torrents if the interface goes down.
- **Web UI:** strict CSP, HttpOnly + SameSite=Strict session cookie, CSRF token on cookie-authenticated writes, no API keys in URLs (except the event stream token, which is short-lived).
- **Audit:** every key creation, revocation, scope change, config import and restore is written to the activity log with actor and IP.

## Sonarr and Radarr migration

A migration wizard reads an existing Sonarr v4 and/or Radarr v5 instance over its API (URL + API key), shows exactly what will be imported, and creates everything in Fetchbox in one dry-runnable job. Nothing on the \*arr side is changed, so users can run both side by side until they're confident.

| \*arr concept | Fetchbox target | Notes |
| --- | --- | --- |
| Series / movies (incl. monitored flags, tags) | `Title`, `WantedItem` | Matched by TVDB/TMDB id; monitor mode mapped where it exists |
| Episode files / movie files | `MediaFile` (registered in place) | Paths translated through a mapping step; existing files are marked acquired + locked |
| Quality profiles + quality definitions | `QualityProfile` ladder + size bounds | Allowed list, order, cutoff and upgrade flag carry over |
| Custom formats + scores | `CustomFormat` + profile scores | Condition types that Fetchbox supports map 1:1; unsupported ones are listed in the report |
| Root folders | `Destination` on the local node | Checked for access before import |
| Indexers (Torznab, built-in) | `Indexer` | Torznab carries over directly; Prowlarr-synced indexers can point at Prowlarr's Torznab URLs |
| Download clients | `DownloadClient` + path mappings | Remote path mappings carry over |
| Naming settings | `NamingTemplate` | Token syntax translated; unknown tokens reported |
| Tags, restrictions / release profiles | Tags, custom formats with negative scores | Best effort |
| Notifications / connections | `NotificationTarget`, `Integration` | Webhook, Discord, Plex, Jellyfin, Emby |
| Blocklist, history | `BlocklistEntry`, `HistoryEvent` | Optional; history imported as read-only |

The wizard ends with a report listing every item imported, skipped and approximated, and offers to disable the matching \*arr monitors once the user confirms Fetchbox is handling downloads, to avoid both apps grabbing the same release.

## Delivery phases and open decisions

Build in five phases, each ending at a gate that proves one end-to-end promise; movies come before TV because the movie pipeline is the TV pipeline without season logic.

&#91;embedded content: delivery roadmap · 5 phases, 5 gates\]

A phase starts only when the previous gate passes. After v1.0 the roadmap items from the scope table (remote storage agents, automation rules, subtitles, more notifiers) follow in priority order.

### Open decisions

| Decision | Options | Recommendation |
| --- | --- | --- |
| Product name | Fetchbox | Decided: Fetchbox. Confirm the name is free on GitHub, PyPI and Docker Hub before the repo goes public |
| Core language | Python (libtorrent bindings, FastAPI) vs Go (single static binary, anacrolix/torrent) | Python: libtorrent is the more mature engine and matches existing tooling; revisit only if NAS memory use is a problem |
| TMDB access | Each user supplies a free key vs a hosted proxy key | User key in v1 (no server to run); proxy later if onboarding friction is high |
| Indexer definition format | Own YAML schema and definitions | Decided: own schema and own definitions, written from scratch; nothing copied from Cardigann, Jackett or Prowlarr |
| Fetchbox's license | MIT | Decided: MIT. No code, definitions or data files are reused from Sonarr, Radarr, Prowlarr or Jackett; every dependency must have an MIT-compatible license |
| Frontend stack | Svelte, React, vanilla JS | Any SPA that builds to static files; choose by what you'll maintain fastest |
| PostgreSQL | v1 vs later | Keep the schema portable from day one, test both in CI, document Postgres as supported in v1.1 |
| Users | Single admin vs multiple users with roles | Single admin + API keys in v1; the scope system already supports roles later |
