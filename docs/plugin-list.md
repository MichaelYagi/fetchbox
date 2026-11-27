# Plugins

Plugins add to Fetchbox: a new kind of media, something extra for what you already have,
or one more service. Install one in **Settings > Plugins**: paste its address and click
**Install** (or *Install from a file* with its `.whl`), then fill in its settings and turn
it on. Plugins run with Fetchbox's full access, so install only ones you trust. To write
your own, see [Writing Fetchbox plugins](plugins.md).

The addresses below are from Fetchbox 0.10.2's release. Each Fetchbox release carries the
newest version of these two: on the [releases page](https://github.com/MichaelYagi/fetchbox/releases),
under *Assets*, copy the `fetchbox_subtitles-…whl` or `fetchbox_comics-…whl` link.

## Subtitles

Subtitles from [OpenSubtitles](https://www.opensubtitles.com) for your movies and episodes,
in the languages you choose, saved next to each file (`Movie.en.srt`) so Plex and other
players find them. New files get theirs when they're imported; files still missing one are
tried again daily; *Search subtitles* on a title page gets them at once.

- **Needs:** a free OpenSubtitles account's API key (Profile > API consumers > New
  consumer). Your username and password are optional: they raise the daily download limit.
- **Address:**
  `https://github.com/MichaelYagi/fetchbox/releases/download/v0.10.2/fetchbox_subtitles-0.1.1-py3-none-any.whl`
- **Source:** [`plugins/fetchbox-subtitles`](../plugins/fetchbox-subtitles)

## Comics

Comic series as a media kind of their own: added from the Add page's **Comics** button,
issues on the Calendar, searched for on your indexers, downloaded, filed as
`Saga (2012)/Saga #054 (2018).cbz` and upgraded (CBZ, CB7, CBR, PDF). A release of a whole
volume counts for its issues.

- **Needs:** a free [ComicVine](https://comicvine.gamespot.com/api/) API key (for series and
  issues), and indexers that carry comics (the Comics category, 7030).
- **Address:**
  `https://github.com/MichaelYagi/fetchbox/releases/download/v0.10.2/fetchbox_comics-0.1.0-py3-none-any.whl`
- **Source:** [`plugins/fetchbox-comics`](../plugins/fetchbox-comics)

## In Docker

`FETCHBOX_PLUGINS` installs plugins when the container starts, into `/config`, so they come
back after an image update (one already installed isn't fetched again):

```sh
-e FETCHBOX_PLUGINS=https://github.com/MichaelYagi/fetchbox/releases/download/v0.10.2/fetchbox_comics-0.1.0-py3-none-any.whl
```

Several are separated by commas. Being listed approves them: they run once installed, and
ask for their settings in Settings > Plugins.

## Adding yours

Written one? Open a pull request adding it to this page, with what it does, what it needs,
where to get its wheel and its source.
