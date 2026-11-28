# Writing Fetchbox plugins

A plugin adds to Fetchbox without changing it: subtitles for your movies, a new kind of thing to collect (comics, audiobooks), a connection to one more service. You write only what's specific to your plugin; searching, grabbing, the queue, importing, renaming, history, notifications and health come from Fetchbox.

This is the plugin API, **version 1**. Within API 1 it only grows: nothing you use is removed, renamed or changes meaning.

> **Installing.** In Fetchbox: Settings > Plugins, *Install from a file* with a plugin's wheel (`.whl`), or paste its address (a release's `.whl` link) and click Install. It asks for its settings when it needs any. Or copy the `.whl` into the data folder's `plugins/drop` (in Docker, `/config/plugins/drop`): it's installed within a minute, then waits in Settings > Plugins to be turned on; a newer version of an installed plugin replaces it and stays on or off as it was, and a file that can't be installed is moved to `drop/failed` with the reason in Health. In Docker, `FETCHBOX_PLUGINS` takes wheel addresses, comma-separated, and installs them at start into `/config`, so they survive image updates. `fetchbox serve --no-plugins` starts with every plugin off. A plugin that fails five times within an hour pauses for an hour; Health and Settings > Plugins say why. The plugins there are, and their addresses: [Plugins](plugin-list.md). Every class and function of the API: [the API reference](plugin-api.md).

## Quick start

Fetchbox and the plugin API aren't on PyPI: install both from a Fetchbox release's files
(the [releases page](https://github.com/MichaelYagi/fetchbox/releases); use the newest
version's file names), with pytest for the test kit:

```sh
python -m venv .venv && . .venv/bin/activate
R=https://github.com/MichaelYagi/fetchbox/releases/download/v0.10.8
pip install "$R/fetchbox_plugin_api-1.0.0-py3-none-any.whl" \
            "$R/fetchbox-0.10.8-py3-none-any.whl" "pytest>=8" "pytest-asyncio>=0.24"

fetchbox plugin new hello             # fetchbox-hello/: an add-on that already works
cd fetchbox-hello
fetchbox plugin test .                # its tests: a real Fetchbox, offline, in seconds
fetchbox plugin dev .                 # Fetchbox in your browser with the plugin loaded
uv build --wheel -o dist .            # dist/fetchbox_hello-0.1.0-py3-none-any.whl: install it
```

The new project's `pyproject.toml` points uv at the same release's files (under
`[tool.uv.sources]`), so in the project `uv sync` also works, and so does its CI workflow.
To move to a newer Fetchbox, change the version in those two addresses.

`fetchbox plugin new hello` makes an add-on that writes `hello.txt` beside every imported movie or episode. With `plugin dev` running, change the text it writes and save: the plugin is loaded again at once, with no restart.

`fetchbox plugin check .` checks it against the API and says how to fix each problem.

## What a plugin is

A plugin is an ordinary Python package. It has one entry point and one `Plugin` class that lists what it contributes:

| Building block | Use it when | You write | Fetchbox gives you |
|---|---|---|---|
| **Add-on** | You add something to titles Fetchbox already has | Event handlers, actions, scheduled jobs | Events, sidecar files that follow their owner, settings, the job list |
| **Media kind** | People collect a new kind of thing | Metadata lookup, release name parsing, file rules, naming | An Add page button, title pages, the Calendar, and the download pipeline: search, grab, download, import |
| **Provider** | You connect one more service (an indexer, a notifier) | A client class with `test()` | Settings form, Test button, Health |

A provider is a class with Fetchbox's provider contract, listed in `contributes` like the rest: offered in Settings beside the built-in ones while the plugin runs (see [Providers](#providers)).

### The package

```
fetchbox-hello/
  pyproject.toml
  src/fetchbox_hello/__init__.py   the Plugin class and what it contributes
  tests/                           tests with the test kit
```

```toml
[project]
name = "fetchbox-hello"                       # plugins are named fetchbox-<name>
dependencies = ["fetchbox-plugin-api>=1,<2"]  # the API; never fetchbox itself

[project.entry-points."fetchbox.plugins"]
hello = "fetchbox_hello:HelloPlugin"          # named after the plugin's id

[dependency-groups]
dev = ["fetchbox", "pytest>=8", "pytest-asyncio>=0.24"]   # the test kit runs Fetchbox
```

Import everything from `fetchbox_plugin` (the `fetchbox-plugin-api` package). Anything imported from elsewhere in `fetchbox` is internal and may change in any release; `plugin check` flags it.

### The Plugin class

```python
from fetchbox_plugin import Permission, Plugin

class HelloPlugin(Plugin):
    id = "hello"                     # permanent: its settings, data and URLs hang off it
    name = "Hello"
    description = "Writes hello.txt beside every imported movie or episode."
    fetchbox_api = 1                 # the API version it's written for
    homepage = "https://github.com/you/fetchbox-hello"
    permissions = {Permission.LIBRARY_FILES}
    contributes = [Hello]            # media kinds, add-ons and providers
```

An id is 2 to 40 lowercase letters, digits, `-` or `_`, starting with a letter.

**Permissions** are shown in plain words before a plugin is installed. They are declared, not enforced: API 1 has no sandbox. `plugin check` compares them with what your code uses.

| Permission | Shown as | Needed when the code uses |
|---|---|---|
| `NETWORK` | Uses the network | `ctx.http`, or a network library |
| `LIBRARY_FILES` | Reads and writes files in your library folders | `ctx.files` |
| `LIBRARY` | Changes what's wanted and searches for it | `ctx.library.wanted`, `ctx.library.search` |
| `NOTIFY` | Sends messages through your notification targets | `ctx.notify` |

## Add-ons

```python
from fetchbox_plugin import Addon, PublicEvent, action, every, on

class Subtitles(Addon):
    id = "subtitles"
    name = "Subtitles"
    applies_to = {"movie", "series"}   # media kinds it works on (leave out for all)
    settings = SubtitleSettings        # a Settings model (below)

    @on("file.imported")
    async def fetch(self, ctx, event: PublicEvent):
        for file_id in event.file_ids:
            ...

    @action("Search subtitles", on="item")      # a button on each movie or episode
    async def search_now(self, ctx, item): ...

    @every(hours=24)                            # a scheduled job, in the job list
    async def retry_missing(self, ctx): ...
```

- **`@on(*events)`** runs for each of these events, for titles of the kinds in `applies_to`. Each run is a background job, after the event's own work is done, so a slow or failing add-on never delays an import. A failure is retried like any job and never stops another add-on.
- **`@action(label, on="title" | "group" | "item")`** is a job run on request, given the title, group or item it was asked for. **`on="library"`** (Fetchbox 0.10.4 or later) is one button on your plugin's card in Settings > Plugins, for the whole library: `async def m(self, ctx)`. Return `{"message": "..."}` and the card shows it when the job ends.
- **`min_fetchbox = "0.10.4"`** on your `Plugin` says which Fetchbox it needs at least; an older one refuses it and says so.
- **`@every(days=, hours=, minutes=)`** is a schedule, run at most every 5 minutes. It's listed with your plugin's name; people can change its interval or run it now.
- Each job has a 10 minute time limit. A job may return a dict: it's kept as the job's result.
- `self.settings` is your add-on's validated settings.

Declaring something wrong (an unknown event, a bad level, an interval under 5 minutes) fails when the class is defined, with what to change.

### Events

Every event has `id`, `type`, `occurred_at`, `title_id`, `kind` (the title's media kind), `file_ids` (with `event.file` for the first) and `data`.

| Event | When | In `data` |
|---|---|---|
| `title.added`, `title.updated`, `title.removed` | A title is added, changed, or removed (its files stay) | `name`, `year`; `fields` on update |
| `release.grabbed`, `release.blocklisted` | A release is sent to a download client, or blocklisted | `release_title`, `indexer`, `quality`, `reason` |
| `download.completed`, `download.failed` | A download finishes or fails | `name`, `save_path` / `error` |
| `file.imported` | Files are filed into the library (an album: every track) | `path`, `quality`, `destination_id` |
| `file.upgraded` | A file replaced a worse copy | `path`, `replaced` |
| `file.renamed` | A file was renamed or moved by the naming | `old`, `new`, `destination_id` |
| `file.deleted` | A file was deleted or recycled | `path`, `from_disk` |
| `library.folder_changed` | Files in library folders changed | `folders` |
| `system.started`, `system.stopping` | Fetchbox starts or begins shutting down | `version` |

### Sidecar files

A sidecar is a file that belongs to a media file: subtitles, lyrics, artwork. `await ctx.files.add_sidecar(file_id, "en.srt", data)` writes `<the file's name>.en.srt` beside it. From then on Fetchbox renames it with the file, moves it with a folder change, recycles it and deletes it together with its owner. You never clean up after a rename.

A suffix can't be a video or audio type, and a plugin can't replace or remove another plugin's sidecar.

## `ctx`: what your code is given

Every method you write receives `ctx`, the only way a plugin talks to Fetchbox. It's typed (`fetchbox_plugin.Context`), so editors complete it and mypy checks it.

| `ctx.` | What it does |
|---|---|
| `http` | An async HTTP client with Fetchbox's timeouts and a User-Agent naming your plugin. GETs are retried on connection errors and 429/502/503/504 (respecting a short Retry-After); POSTs are never sent twice. Declare a host's rate limit once: `ctx.http.limit("api.example.com", per_second=1)` |
| `cache` | Answers kept for a while: `await ctx.cache.remember(key, seconds, fetch)` |
| `store` | A small key-value store in Fetchbox's database (in backups): `await ctx.store.get("last_sync")`, `set`, `delete`, `keys(prefix)`. JSON values up to 64 KB |
| `folder` | Your plugin's own folder in Fetchbox's data folder, for downloaded models, indexes, temp files |
| `log` | A logger tagged with your plugin's id: `ctx.log.info("synced", count=3)` |
| `files` | `path(file_id)`; `add_sidecar`, `sidecars(file_id, suffix=)`, `remove_sidecar` |
| `library` | `titles(kind=)`, `title(id)`, `items(title_id)`, `item(id)`, `files(title_id)`, `file(id)`: plain read-only values (`TitleInfo`, `ItemInfo` with an episode's season and number, `FileInfo`). `wanted(item_id, monitored)` and `search(item_id)` act as the web page does |
| `jobs` | Your own background jobs: `@ctx.jobs.job("sync")` and `await ctx.jobs.enqueue("sync", payload)` |
| `notify` | `await ctx.notify(title, body, level="info")`: a message to the notification targets that chose plugin messages |

When something isn't there (any more), `ctx` raises `NotFound` (a `LookupError`); when a plugin may not do something, such as changing another plugin's sidecar, `Refused` (a `PermissionError`). Both come from `fetchbox_plugin`.

## Settings

A plugin's settings are one model; Fetchbox draws the settings form from it, with validation, help text and a Test button. You write no form code.

```python
from fetchbox_plugin import Field, Folder, Secret, Settings

class SubtitleSettings(Settings):
    api_key: Secret = Field(title="OpenSubtitles API key", help="Free at opensubtitles.com")
    languages: list[str] = Field(["en"], title="Languages, best first")
    folder: Folder = Field("", title="Extra subtitles folder")
    max_per_day: int = Field(100, title="Downloads a day", ge=1, le=1000)

    async def test(self, ctx) -> str:          # optional: the Test button
        r = await ctx.http.get("https://api.opensubtitles.com/api/v1/infos/user",
                               headers={"Api-Key": self.api_key.value})
        r.raise_for_status()
        return "The key works"
```

| Field type | In the settings form |
|---|---|
| `str`, `int`, `float`, `bool` | Text, number, checkbox; `Field(title, help, ge, le)` sets label, hint and limits |
| `Literal[...]`, enums | A dropdown |
| `list[str]` | A box, one per line |
| `Field(..., choices={"en": "English", ...})` | Names to pick from, kept as their values: on a `str` a dropdown; on a `list[str]`, several, in order (move one earlier with ↑). Fetchbox 0.10.4 or later |
| `Secret` | A password field, never sent back to the browser; read it with `.value`. It never shows in logs or f-strings |
| `Folder` | Fetchbox's library folder picker |
| `Url` | An address field with a reachability test |

Unknown fields are refused, and settings (their defaults too) are validated on every change. Add-ons and media kinds both declare theirs with `settings = MySettings`; when a plugin runs, `self.settings` holds the validated values. A plugin whose settings aren't complete isn't loaded, and says which setting is missing.

## Media kinds

A media kind describes its things as a tree of up to three levels, and answers six questions about them.

| Kind | Title | Group | Item (what's wanted and downloaded) |
|---|---|---|---|
| TV (core) | Show | Season | Episode |
| Music (core) | Artist | Album | Track (grabbed per album) |
| Comics (plugin) | Series | Volume | Issue |

```python
from fetchbox_plugin import Hit, Item, MediaKind, Naming, Parsed, Qualities, TitleDetails

class Comics(MediaKind):
    id = "comics"                                  # stored with titles: 2 to 10 characters
    name = "Comics"
    levels = ("series", "volume", "issue")         # the words the web page uses
    file_types = {".cbz", ".cbr", ".pdf"}
    grab_level = "item"                            # or "group" (music grabs whole albums)
    qualities = Qualities(["CBZ digital", "CBZ scan", "CBR", "PDF"])   # best first
    naming = Naming("{series} ({start_year})/{series} #{issue:03}",
                    tokens={"series", "start_year", "volume", "issue", "title"})

    async def search(self, ctx, text) -> list[Hit]: ...            # 1 the Add page's search
    async def title(self, ctx, ref) -> TitleDetails: ...           # 2 the tree and release dates
    def queries(self, title, items): ...                           # 3 optional
    def parse(self, release_name) -> Parsed | None: ...           # 4 read a release name
    def pick(self, files, items): ...                              # 5 optional
    def name_tokens(self, title, item) -> dict: ...                # 6 values for the naming
```

| # | Method | Answers | Default |
|---|---|---|---|
| 1 | `search` | Titles matching what someone typed: `Hit(ref, name, year, overview, poster_url)` | required |
| 2 | `title` | A title's groups and items with numbers, names and release dates (`TitleDetails`); asked again on refresh | required |
| 3 | `queries` | What to ask indexers for wanted items | `"<title> <number>"` per item |
| 4 | `parse` | From a release name: title words, item numbers, year, quality (`Parsed`); `None` when it isn't yours | required |
| 5 | `pick` | Which file of a download is which item: `{file path: item ref}` | one item: the largest file of your types; several: numbers in file names (`061` is 61) |
| 6 | `name_tokens` | The values your naming's tokens stand for | required |

Each title of the kind has its own accepted qualities and cutoff, on its page: an item whose file is below the cutoff is upgraded when a better release turns up. A release your `parse` reads as a whole group (a group number, no item numbers) covers that group's wanted items. Titles are read again from your source once a day (`title()`, so new items appear, wanted as each title's monitor setting says), searched every 6 hours, and offered the RSS releases too.

`movie`, `series` and `music` are built in and can't be taken. A kind is checked when it's declared, and every problem comes back in one message. A naming pattern can't use a token it doesn't declare.

## Testing

The test kit runs a real Fetchbox in each test, offline, in a temporary folder, with your plugin loaded. Add this to `tests/conftest.py`:

```python
pytest_plugins = ["fetchbox_plugin.testing"]
```

```python
async def test_hello_is_written(fetchbox_app):
    app = await fetchbox_app(plugins=[HelloPlugin], settings={"hello": {"text": "Hi"}})
    f = await app.add_file("Dune", 2021)       # a movie in the library, then file.imported
    assert f.path.with_suffix(".hello.txt").read_text() == "Hi"

async def test_any_event(fetchbox_app):
    app = await fetchbox_app(plugins=[HelloPlugin])
    [ran] = await app.emit("title.added", title_id="t1", kind="movie", name="Arrival")
    assert ran.status == "succeeded"
```

| Helper | Gives |
|---|---|
| `fetchbox_app(plugins=, settings=)` | A running Fetchbox with your plugins; `settings` per plugin id (or per contribution id) |
| `app.add_file(name, year, kind=, season=, episode=, tmdb_id=, imdb_id=, path=, quality=, imported=True)` | A title with a file in its library folder, as if downloaded (for a series, `season` and `episode` make it that episode's); then `file.imported`, its handlers run |
| `app.emit(type, title_id=, kind=, file_ids=, **data)` | Any event, its handlers run and waited for. A handler that raised fails the test with its error; `check=False` gives each outcome instead |
| `app.run(plugin_id, method, item=None)` | Runs an `@every` or `@action` method now, as a job, and waits: `.status`, `.result`, `.error` |
| `app.fake_http(plugin_id, handler)` | Answers your plugin's `ctx.http` with `handler(request) -> httpx.Response` instead of the network; gives the list of requests it made |
| `app.client` | Fetchbox's API (`/api/v1`), signed in |
| `app.ctx(plugin_id)` | Your plugin's `ctx`, to look at what it stored |
| `app.library_file(path, kind=)` | Where a file is in a library folder |
| `parse_cases(kind, table)`, `check_parse` | Table-driven tests for `parse` (below) |

```python
CASES = """
# release name                 | title | numbers     | year | quality
Saga 061 (2023) (Digital).cbz  | Saga  | 61          | 2023 | CBZ
Saga #1-6 (2012) TPB.cbr       | Saga  | 1,2,3,4,5,6 | 2012 | CBR
Not a comic.mkv                | -
"""

@pytest.mark.parametrize(("name", "want"), parse_cases(Comics, CASES))
def test_parse(name, want):
    check_parse(Comics, name, want)
```

An empty field isn't checked; `-` on its own means `parse` should give `None`. A line starting with `#` is a comment. The table can also be a file.

## Providers

A provider connects Fetchbox to one more service of a kind it already has: a notifier, an
indexer, a download client, a media server. It's Fetchbox's own provider class, not part of
the plugin API, so a provider plugin depends on `fetchbox` and may need changes when
Fetchbox changes; within API 1 everything else here doesn't. A notifier, complete:

```python
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel

from fetchbox.core.providers import TestResult
from fetchbox.providers.notifiers import Notification, NotifierProvider
from fetchbox_plugin import Plugin


class Webhook(NotifierProvider):
    kind = "my-webhook"               # unique among notifiers

    class Settings(BaseModel):        # the form in Settings > Notifications
        url: str

    async def send(self, n: Notification) -> None:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post(self.settings.url, json={"title": n.title, "body": n.body})
            r.raise_for_status()

    async def test(self) -> TestResult:  # the Test button
        return TestResult(ok=self.settings.url.startswith("https://"), message="checked")


class WebhookPlugin(Plugin):
    id = "my-webhook"
    name = "My webhook"
    contributes: ClassVar[list[type[Any]]] = [Webhook]
```

With the plugin running, *My webhook* is offered in Settings > Notifications. When it's
turned off, targets set up with it stay listed and say their plugin is off; they're not
used, and not offered for new ones. Indexers (`fetchbox.providers.indexers.IndexerProvider`)
and download clients (`fetchbox.providers.download_clients.DownloadClientProvider`) work the
same way; the built-in ones in `src/fetchbox/providers` are the examples.

## Troubleshooting

| What you see | What it means |
|---|---|
| *… is written for plugin API 2; this Fetchbox supports API 1* | The plugin was written for an API this Fetchbox doesn't have. Update Fetchbox, or use a version of the plugin for API 1 |
| The plugin is listed but not running | It isn't approved or turned on yet (Settings > Plugins), or it failed to start: its error is shown there and in Health |
| *paused for an hour after failing repeatedly* | Five failures within an hour. Its last error says why; it starts again on its own, or when turned off and on |
| Fetchbox won't start after installing a plugin | Start it with every plugin off: `fetchbox serve --no-plugins` (the desktop app's program takes the same arguments, e.g. `/Applications/Fetchbox.app/Contents/MacOS/Fetchbox serve --no-plugins`), then remove the plugin in Settings > Plugins |
| A media kind finds nothing | Your indexers may not carry it. A kind searches its `categories` (Comics: 7030) on the indexers in Settings > Sources; add one that has them |
| `plugin test` says *No module named pytest* | Install `pytest` and `pytest-asyncio` into the environment Fetchbox is in |
| `uv sync` can't find `fetchbox-plugin-api` | It isn't on PyPI. A project made by `plugin new` (Fetchbox 0.10.3 or later) points uv at a release's files under `[tool.uv.sources]`; add those two lines to an older one, or install from a release's files (see [Quick start](#quick-start)) |

Learned the hard way:

- **Some services refuse Fetchbox's User-Agent.** `ctx.http` names your plugin
  (`Fetchbox/<version> (plugin <id>)`), and OpenSubtitles' gateway blocks any User-Agent
  with "fetch" in it, answering 403 for everything. Services that ask for a particular form
  (OpenSubtitles: `AppName vVersion`) may need your own: pass `headers={"User-Agent": ...}`
  with the request, as the Subtitles plugin does.
- **Show the service's own words.** When a request is refused, put what the service said
  in your error (its JSON `message`), not a guess: a 403 isn't always a wrong password.
- **Keep to rate limits** with `ctx.http.limit(host, per_second=...)` once, rather than
  sleeping between requests.
- **Saving settings reloads the plugin.** Anything kept only in memory is gone after;
  keep what must last in `ctx.store` (or `ctx.cache` for answers that may be fetched
  again).

## Reference plugins

Two complete plugins in this repository, built with the API alone and tested with the kit, to read and copy from:

- [`plugins/fetchbox-subtitles`](../plugins/fetchbox-subtitles): an add-on. Subtitles from OpenSubtitles for imported movies and episodes, as sidecars; a daily retry, an action, settings with a Test button, and a fake OpenSubtitles in its tests.
- [`plugins/fetchbox-comics`](../plugins/fetchbox-comics): a media kind. Comic series and issues from ComicVine, a release name parser with a table of real names, and the naming.

## Commands

| Command | What it does |
|---|---|
| `fetchbox plugin new <name> [--kind addon\|media] [--dir DIR]` | A new plugin project that works, passes its tests and `plugin check`: the package, the plugin, tests, a README and a CI workflow |
| `fetchbox plugin test [path] [-- pytest args]` | Runs the plugin's tests with the test kit |
| `fetchbox plugin check [path]` | Checks it against API 1: the package and entry point, the Plugin class, what it contributes, settings models, permissions against the code, imports of Fetchbox's internals. Prints the fix for each problem; exit code 1 on errors |
| `fetchbox plugin dev [path] [--port 8788] [--data DIR] [--no-browser]` | Fetchbox with the plugin loaded from source and a sample movie, in a throwaway data folder, offline. Saving a file of the plugin loads it again; a mistake is printed and it carries on |

Without Fetchbox installed, `python -m fetchbox_plugin check <path>` and `python -m fetchbox_plugin new <name>` work with the API package alone.

## Versions and trust

- **API versions.** The plugin API has its own version, separate from Fetchbox's. A plugin declares the one it targets (`fetchbox_api = 1`). A Fetchbox that doesn't support it won't load it, and says which version it needs.
- **What counts as the API.** Everything exported from `fetchbox_plugin` and `fetchbox_plugin.testing` is API. Anything else in `fetchbox` is internal.
- **Trust.** Plugins run inside Fetchbox with its full access: there's no sandbox in API 1. People should install only plugins from authors they trust. The install screen shows your permissions in plain words for that reason.
