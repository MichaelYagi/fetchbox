# Writing Fetchbox plugins

A plugin adds to Fetchbox without changing it: subtitles for your movies, a new kind of thing to collect (comics, audiobooks), a connection to one more service. You write only what's specific to your plugin; searching, grabbing, the queue, importing, renaming, history, notifications and health come from Fetchbox.

This is the plugin API, **version 1**. Within API 1 it only grows: nothing you use is removed, renamed or changes meaning.

> **Where it is now.** You can write, test, check and run plugins today (`fetchbox plugin new / test / check / dev`, below). Installing them from Settings → Plugins comes in a coming release, together with the parts of the web page plugins add: action buttons, panels, settings tabs, and a media kind's Add button and Library tab.

## Quick start

```sh
pip install fetchbox                  # or use the desktop app's bundled fetchbox command
fetchbox plugin new hello             # fetchbox-hello/: an add-on that already works
cd fetchbox-hello
fetchbox plugin test .                # its tests: a real Fetchbox, offline, in seconds
fetchbox plugin dev .                 # Fetchbox in your browser with the plugin loaded
```

`fetchbox plugin new hello` makes an add-on that writes `hello.txt` beside every imported movie or episode. With `plugin dev` running, change the text it writes and save: the plugin is loaded again at once, with no restart.

`fetchbox plugin check .` checks it against the API and says how to fix each problem.

## What a plugin is

A plugin is an ordinary Python package. It has one entry point and one `Plugin` class that lists what it contributes:

| Building block | Use it when | You write | Fetchbox gives you |
|---|---|---|---|
| **Add-on** | You add something to titles Fetchbox already has | Event handlers, actions, scheduled jobs | Events, sidecar files that follow their owner, settings, the job list |
| **Media kind** | People collect a new kind of thing | Metadata lookup, release name parsing, file rules, naming | The download pipeline (with the coming release: Add page, Library tab, title pages) |
| **Provider** | You connect one more service (an indexer, a notifier) | A client class with `test()` | Settings form, Test button, Health |

Providers keep using Fetchbox's existing `fetchbox.providers` entry point.

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
- **`@action(label, on="title" | "group" | "item")`** is a job run on request, given the title, group or item it was asked for.
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
| `library` | `titles(kind=)`, `title(id)`, `files(title_id)`, `file(id)`: plain read-only values. `wanted(item_id, monitored)` and `search(item_id)` act as the web page does |
| `jobs` | Your own background jobs: `@ctx.jobs.job("sync")` and `await ctx.jobs.enqueue("sync", payload)` |
| `notify` | `await ctx.notify(title, body, level="info")`: a message to the notification targets that chose plugin messages |

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
| `list[str]` | A list people can reorder |
| `Secret` | A password field, never sent back to the browser; read it with `.value`. It never shows in logs or f-strings |
| `Folder` | Fetchbox's library folder picker |
| `Url` | An address field with a reachability test |

Unknown fields are refused, and settings are validated on every change.

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
| `app.add_file(name, year, kind=, path=, quality=, imported=True)` | A title with a file in its library folder, as if downloaded; then `file.imported`, its handlers run |
| `app.emit(type, title_id=, kind=, file_ids=, **data)` | Any event, its handlers run and waited for. A handler that raised fails the test with its error; `check=False` gives each outcome instead |
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
