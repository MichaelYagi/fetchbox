# Plugins

Plugins add to Fetchbox: a new kind of media, something extra for what you already have,
or one more service. Install one in **Settings > Plugins**: paste its address and click
**Install** (or *Install from a file* with its `.whl`), then fill in its settings and turn
it on. Plugins run with Fetchbox's full access, so install only ones you trust. To write
your own, see [Writing Fetchbox plugins](plugins.md).

The addresses below are from Fetchbox 0.10.4's release. Each Fetchbox release carries the
newest version of these two: on the [releases page](https://github.com/MichaelYagi/fetchbox/releases),
under *Assets*, copy the `fetchbox_subtitles-…whl` or `fetchbox_comics-…whl` link.

## Subtitles

Subtitles from [OpenSubtitles](https://www.opensubtitles.com) for your movies and episodes,
in the languages you choose, saved next to each file (`Movie.en.srt`) so Plex and other
players find them. New files get theirs when they're imported; files still missing one are
tried again daily. *Find missing subtitles* gets them at once, for an episode, a season, a
movie or show, or the whole library (on its card in Settings > Plugins), files that were there
before the plugin included. Version 0.2.0 needs Fetchbox 0.10.4 or later.

- **Needs:** a free OpenSubtitles account's API key (Profile > API consumers > New
  consumer). Your username and password are optional: they raise the daily download limit.
- **Address:**
  `https://github.com/MichaelYagi/fetchbox/releases/download/v0.10.4/fetchbox_subtitles-0.2.0-py3-none-any.whl`
- **Source:** [`plugins/fetchbox-subtitles`](../plugins/fetchbox-subtitles)

## Comics

Comic series as a media kind of their own: added from the Add page's **Comics** button,
issues on the Calendar, searched for on your indexers, downloaded, filed as
`Saga (2012)/Saga #054 (2018).cbz` and upgraded (CBZ, CB7, CBR, PDF). A release of a whole
volume counts for its issues.

- **Needs:** a free [ComicVine](https://comicvine.gamespot.com/api/) API key (for series and
  issues), and indexers that carry comics (the Comics category, 7030).
- **Address:**
  `https://github.com/MichaelYagi/fetchbox/releases/download/v0.10.4/fetchbox_comics-0.1.0-py3-none-any.whl`
- **Source:** [`plugins/fetchbox-comics`](../plugins/fetchbox-comics)

## Copying the file instead

Copy a plugin's `.whl` into the `plugins/drop` folder of Fetchbox's data folder (Settings >
General shows where that is; in Docker, `/config/plugins/drop`). Fetchbox installs it within
a minute and lists it in Settings > Plugins, where you turn it on. A newer version of a
plugin you have replaces it, on or off as it was. Installed files move to `drop/installed`;
one that can't be installed moves to `drop/failed`, and Health says why.

## In Docker

`FETCHBOX_PLUGINS` installs plugins when the container starts, into `/config`, so they come
back after an image update (one already installed isn't fetched again):

```sh
-e FETCHBOX_PLUGINS=https://github.com/MichaelYagi/fetchbox/releases/download/v0.10.4/fetchbox_comics-0.1.0-py3-none-any.whl
```

Several are separated by commas. Being listed approves them: they run once installed, and
ask for their settings in Settings > Plugins.

## Adding yours

Written one? Open a pull request adding it to this page, with what it does, what it needs,
where to get its wheel and its source.
