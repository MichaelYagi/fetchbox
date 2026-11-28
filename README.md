# Fetchbox

Self-hosted media manager for movies, TV and music: the Sonarr, Radarr and Lidarr workflow
in one app. Find a title, pick a quality, and Fetchbox searches your indexers, downloads it,
names and files it in your library, and tells Plex, Jellyfin or Emby to pick it up. It
upgrades what it has when a better copy turns up, and plugins add more: subtitles, comics,
or a kind of media of your own.

**One app, from search to Plex.** Fetchbox has its own torrent engine, so there's no
download client to install, connect or keep running beside it. Add a title and it's
found, downloaded and filed in your library folder; with Plex connected, it's showing there
a minute later, as Plex is asked to rescan just that folder. The usual setup does the same
with Sonarr, Radarr and Lidarr plus a separate qBittorrent or SABnzbd, all wired to one
another; here it's Fetchbox and your indexers (Prowlarr, say). It works with those clients
too, if you already run one.

What it does:

- **Movies, TV and music**, one workflow: find a title (TMDB for films and shows,
  MusicBrainz for artists and albums), choose its quality, and it's searched for, grabbed,
  downloaded, imported, renamed and filed. New episodes and albums are picked up as they're
  announced, and the Calendar shows what's coming.
- **Straight into Plex:** downloads go into the library folder itself and are filed in
  place, named the way Plex, Jellyfin and Emby match best, and the media server rescans just
  the folder that changed: no waiting for its next scheduled scan.
- **Your Plex watchlist:** press *+ Watchlist* in any Plex app (TV, phone, plex.tv) and
  it's added and downloaded. Fetchbox uses the token your Plex server was added with, or
  Plex's own sign-in for anyone else in the household; optionally it asks you first.
- **Torrents and Usenet:** the built-in engine, or your own qBittorrent, Transmission or
  Deluge; and Usenet through SABnzbd or NZBGet. Indexers come from Prowlarr (torrent and
  Usenet alike), a Torznab feed or a Newznab indexer.
- **Upgrades and repairs:** a better copy replaces the one you have, up to the quality you
  set. *Redownload* swaps a bad copy for another release, *Delete* removes a file, and the
  daily library check reports files that went missing from disk.
- **Your existing library:** *Import what's already here* adopts the media you have without
  moving it, and *Move from Radarr / Sonarr / Lidarr* brings over your titles and their files.
- **Easy to start:** a first-run setup walks through folders, keys, an indexer, downloads
  and Plex, every step optional.
- **Around it:** the Queue and Activity show what's happening, and notifications go to
  email, Discord or a webhook.
- **Plugins** add subtitles, comics, or a kind of media or service of your own; see
  [Plugins](#plugins).

Status: early development — see [PLAN.md](PLAN.md) and the engineering specification.

<img width="1203" height="911" alt="Screenshot 2026-11-27 175701" src="https://github.com/user-attachments/assets/8425ee3a-09bb-492c-9c0f-cecf578b4d85" />

## Getting started

```sh
uv sync
cd web && npm ci && npm run build && cd ..   # the web UI (Node 24); the build isn't checked in
uv run fetchbox serve --data ./data          # then open http://127.0.0.1:8787
```

On first run, choose the admin password in the browser. The terminal also prints a
one-time API key for scripts. Back up `data/secrets.key`: it encrypts stored passwords and
keys.

Other devices on your network open it at `http://<this computer's address>:8787` and sign
in (the first-run password can only be chosen on this computer). To keep it to this
computer only: `--host 127.0.0.1`, `host = "127.0.0.1"` in `data/config.toml`, or untick
"Allow access from other devices" in the desktop tray menu.

### Docker

```sh
docker run -d --name fetchbox -p 8787:8787 -p 6881:6881 -p 6881:6881/udp \
  -e FETCHBOX_ADMIN_PASSWORD=change-me -e PUID=1000 -e PGID=1000 \
  -v fetchbox-config:/config -v /srv/media:/data ghcr.io/michaelyagi/fetchbox
```

`/config` holds the database and settings. `/data` is the library (`Movies/`, `TV/`,
`Music/`). Downloads go straight into the folder they're for (a hidden `.incomplete`
folder there), are filed in place when finished, and seed from where they're filed: one
file each, no copies. Set `PUID`/`PGID` to the owner of your media folder. The
password is only used on first start; change it in the web UI. A compose file is in
`docker/compose.yaml`.

Plugins: `-e FETCHBOX_PLUGINS=<wheel address>,…` installs them at start into `/config`, so
they come back after an image update; or copy a `.whl` into `/config/plugins/drop`. The plugins and their addresses: [Plugins](docs/plugin-list.md).

### Desktop apps

Each release has a Windows installer (and a portable .zip), macOS disk images (Apple
Silicon and Intel), and for Linux a .deb (a service), an AppImage and a plain binary.
Windows and macOS start in the tray / menu bar, open Fetchbox in your browser, and
update themselves from the tray menu. They're unsigned: the bundled README.txt explains
getting past SmartScreen or Gatekeeper. Building them yourself: `packaging/README.md`.

### Recommended setup: a Mac, a NAS and Plex

The setup Fetchbox is used and tested with day to day: one always-on Mac runs Fetchbox
and Plex, and the media lives on a NAS.

```
  NAS (SMB shares)                    Mac (always on)
  ├─ Movies/  ◀──── downloads, ────── Fetchbox desktop app (menu bar)
  ├─ TV/            files, seeds        └─ built-in torrent engine
  └─ Music/                           Plex Media Server ◀── "rescan this folder"
                                      Prowlarr (this Mac or another machine)
```

1. **The Mac.** Install the macOS desktop app. In its menu bar menu, turn on **Start at
   login**, and keep **Allow access from other devices** on to use Fetchbox from a
   phone or another computer. In System Settings, stop the Mac from sleeping on its own
   (downloads and searches pause while it sleeps).
2. **The NAS.** Connect its shares in Finder (Go → Connect to Server, e.g.
   `smb://nas.local/Media`) and add them to your Login Items, so they're mounted at the
   same `/Volumes/...` path after every restart. Movies, TV and music can be on different
   shares.
3. **Library folders.** In Settings → Library, set the Movies, TV shows and Music folders
   to the folders on the NAS (e.g. `/Volumes/Media/Movies`). Already have media there?
   **Import what's already here** adds it without moving anything.
4. **Sources.** In Settings → Sources, paste your TMDB key and add Prowlarr as an indexer
   (see [What you need](#what-you-need)). Leave the built-in download client on: it
   downloads straight into the library folder on the NAS, files the finished download in
   place and seeds from there, so there's one copy of each file and nothing to move
   between a download folder and the library.
5. **Plex.** Point Plex's libraries at the same NAS folders. In Settings → Connections →
   Media servers, add Plex with its address (`http://127.0.0.1:32400` on the same Mac)
   and its token (the form says where to find it), then **Test**. Fetchbox finds which
   Plex library holds each of its folders, even though Plex on a Mac may spell them
   `/Volumes/Macintosh HD/Volumes/...`, and asks Plex to rescan just the folder it
   changed after each import or rename.

What Fetchbox does for this setup by itself:

- **The NAS is off or disconnected:** its folders show as *unmounted* in Settings →
  Library and imports wait until it's back. Nothing is written to the Mac's own disk in
  its place.
- **macOS's `._` files** (written next to files on a share that can't keep a Mac file's
  extra details): Fetchbox doesn't create them for what it imports, never takes them for
  media, and clears leftovers from the library folders after each import and once a day.
- **Updates:** **Install** in Settings → General (or the menu bar menu) downloads the new
  version, replaces the app and restarts it, the server included.

### Behind a reverse proxy

In Settings → Network, list the proxy's address (or its Docker network, e.g.
`172.18.0.0/16`) under **Trusted proxies**, so Fetchbox logs and limits failed sign-ins
by the visitor's real address. Pass the usual forwarding headers:

```nginx
location / {
    proxy_pass http://127.0.0.1:8787;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;                      # the live updates are a WebSocket
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

Caddy sends these by itself: `reverse_proxy 127.0.0.1:8787`.

**Single sign-on** (Authelia, Authentik, oauth2-proxy): turn on "Sign in whoever the
proxy says" with the header your proxy sets (`Remote-User` by default). Fetchbox then
skips its own sign-in for requests from a trusted proxy that carry that header. Only do
this when the proxy checks the login on every request and strips the header from what
visitors send, and make sure Fetchbox can't be reached without going through the proxy.

If Fetchbox is reachable from the internet, also consider **Only send webhooks to public
internet addresses** in the same place.

### What you need

| | Needed for | Where to get it |
| --- | --- | --- |
| **TMDB API key** (free) | Movies and TV: finding titles, years and episode lists | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api); paste it in Settings → Sources |
| MusicBrainz | Music: artists, albums and tracks | Nothing: built in, no key needed |
| TheTVDB key | Optional: anime released by absolute episode number (`Show - 1071`) | [thetvdb.com](https://thetvdb.com/api-information); Settings → Sources |
| **Prowlarr** (or Jackett) | Mainstream movies, shows and music (and comics, with the plugin) | [Prowlarr](https://prowlarr.com), set up with the sites you use. In Settings → Sources → Indexers, add **Prowlarr** with the address you open it at (e.g. `http://192.168.1.10:9696`) and its API key (Prowlarr: Settings → General → Security). Every enabled Prowlarr indexer is searched, torrent and Usenet, including ones added later. For Jackett or a single feed, use **Torznab feed** with that feed's URL |
| Usenet (optional) | Releases from Usenet, alongside torrents | A Usenet indexer (add it as **Newznab**, or in Prowlarr) and a news server account, set up in **SABnzbd** or **NZBGet**; add that client in Settings → Sources → Download clients. Fetchbox sends it each NZB, files the finished download, then removes it from the client |
| Download client | Nothing: the built-in engine works on its own | Optional: qBittorrent, Transmission or Deluge (torrents), SABnzbd or NZBGet (Usenet), in Settings → Sources → Download clients |

Without Prowlarr or Jackett, Fetchbox searches only:

- **Internet Archive** (built in): films whose uploader marked them public domain or
  Creative Commons. Their releases don't state a quality, so add them with the **Any**
  profile.
- **Your own indexer definitions**: Fetchbox's YAML format for sites with a JSON or RSS
  API, added with `POST /api/v1/indexer-definitions`. The format is described in
  `src/fetchbox/providers/indexers/definition.py`. Fetchbox ships no definitions for
  other sites.

### Quality profiles

For movies and TV, **1080p** (the default), **2160p**, **720p** and **480p** each take only
that resolution, preferring better sources: Blu-ray over WEB over HDTV. **Any** also takes
releases that don't state a quality. For music, **Lossless** (FLAC, preferring 24-bit),
**High-quality lossy** (MP3 320 or V0) and **Any audio**. Profiles can be edited through
the API (`/api/v1/quality-profiles`). A plugin's kind of media (like comics) has its
qualities chosen on each title's page instead.

### Where files go

Until you set library folders in Settings → Library, files go to
`~/Downloads/Fetchbox/Movies`, `.../TV` and `.../Music` (Linux: your XDG Downloads
folder). Set `FETCHBOX_DOWNLOADS_ROOT` to change that starting point; in Docker it's `/data`.

### Troubleshooting: "database is locked" or stalls

Start Fetchbox with `FETCHBOX_DB_GUARD=1` (and optionally `FETCHBOX_DB_GUARD_OUT=guard.txt`)
to record what holds the database: write transactions held over a second, a task waiting on
its own write, and any moment the server's event loop is blocked for over half a second,
each with the code responsible. Attach that file to a bug report.

## Plugins

Plugins add to Fetchbox without changing it: subtitles, a new kind of thing to collect,
one more service. Install them in Settings > Plugins.

- [Plugins](docs/plugin-list.md): what there is, and each one's address to install from.
- [Writing Fetchbox plugins](docs/plugins.md): the guide. Start with `fetchbox plugin new hello`.
- [Plugin API reference](docs/plugin-api.md): every class and function plugins can use.

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
