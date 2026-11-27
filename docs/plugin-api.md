# Plugin API reference (API 1)

Everything a plugin can use, from `fetchbox_plugin`. Generated from the API itself by
`plugin-api/scripts/reference.py`; for how the pieces fit together, see the guide,
[Writing Fetchbox plugins](plugins.md). Within API 1 this only grows: nothing is
removed, renamed or changes meaning.

## The plugin

### `Plugin`

`class Plugin`

Subclass once per plugin package and point the ``fetchbox.plugins`` entry point at it.

```python
id: ClassVar[str]  # permanent: settings, stored data and URLs hang off it
name: ClassVar[str]
description: ClassVar[str] = ""
fetchbox_api: ClassVar[int] = 1  # the plugin API version it was written for
min_fetchbox: ClassVar[str | None] = None  # when it needs an API 1 addition
homepage: ClassVar[str | None] = None
version: ClassVar[str | None] = None  # from the package's metadata when not set
permissions: ClassVar[frozenset[Permission] | set[Permission]] = frozenset()
contributes: ClassVar[list[type[Any]]] = []  # media kinds, add-ons and providers
```

### `Permission`

`class Permission(StrEnum)`

What a plugin uses, shown in plain words before it's installed. Declared, not enforced
(API 1 has no sandbox); ``fetchbox plugin check`` compares them with the code.

```python
NETWORK = "network"  # "Uses the network"
LIBRARY_FILES = "library_files"  # "Reads and writes files in your library folders"
LIBRARY = "library"  # "Changes what's wanted and searches for it"
NOTIFY = "notify"  # "Sends messages through your notification targets"
```

### `PERMISSION_WORDS`

`PERMISSION_WORDS`: a dict

- `network`: Uses the network
- `library_files`: Reads and writes files in your library folders
- `library`: Changes what's wanted and searches for it
- `notify`: Sends messages through your notification targets

### `API_VERSION`

`API_VERSION = 1`

## Add-ons

### `Addon`

`class Addon`

Something that reacts to Fetchbox: methods decorated with ``@on`` (events),
``@action`` (buttons), ``@every`` (schedules) and ``@panel`` (title page panels). List it
in your Plugin's ``contributes``; ``settings = MySettings`` gives it settings.

```python
id: ClassVar[str]
name: ClassVar[str]
applies_to: ClassVar[frozenset[str] | set[str] | None] = None  # media kinds; None: all
settings_model: ClassVar[type[Settings] | None] = None
settings: Any = None  # the validated settings, set by Fetchbox before any method runs
```

- **`handlers()-> dict[str, list[tuple[str, Any]]]`**: The add-on's decorated methods by what they are: on, action, every, panel.

### `on`

```python
on(*events: str) -> Callable[[F], F]
```

Run the method for each of these public events: ``async def m(self, ctx, event)``.

### `action`

```python
action(label: str, *, on: Level = item) -> Callable[[F], F]
```

A button on each title, group or item: ``async def m(self, ctx, item)``. `item` is the
title's id, the item's id, or for a group "<title id>:<group>" (a show's season: its
number, "…:2"; shown on season rows from Fetchbox 0.10.4). Or, with
``on="library"``, one button for the whole library on the plugin's card in Settings >
Plugins: ``async def m(self, ctx)`` (Fetchbox 0.10.4 or later). Runs as a job and shows
its progress and result.

### `every`

```python
every(*, days: float = 0, hours: float = 0, minutes: float = 0) -> Callable[[F], F]
```

A scheduled job, shown in Fetchbox's job list: ``async def m(self, ctx)``.

### `panel`

```python
panel(*, on: Level, title: str) -> Callable[[F], F]
```

A panel on the title page: ``async def m(self, ctx, item) -> Panel``. Fetchbox draws
it in its own style, so plugins ship no JavaScript.

### `Panel`

`class Panel`

What a panel shows: a status line, a table, and buttons for the add-on's actions.

```python
status: str = ""
table: list[dict[str, Any]] = field(default_factory=list)
actions: list[str] = field(default_factory=list)  # labels of @action methods
```

### `PublicEvent`

`class PublicEvent(BaseModel)`

One event as plugins see it.

```python
id: str
type: str
occurred_at: datetime
title_id: str | None = None
kind: str | None = Field(default=None, description="The title's media kind")
file_ids: list[str] = Field(default_factory=list)
data: dict[str, Any] = Field(default_factory=dict)
```

- **`file`** → `str | None`: The event's file, when it's about one (``file.imported`` of a movie).

### `PUBLIC_EVENTS`

`PUBLIC_EVENTS`: a dict

- `title.added`: A title was added
- `title.updated`: A title's details or settings changed
- `title.removed`: A title was removed (its files stay on disk)
- `release.grabbed`: A release was sent to a download client
- `release.blocklisted`: A release was blocklisted
- `download.completed`: A download finished
- `download.failed`: A download failed
- `file.imported`: Files were filed into the library (an album: every track)
- `file.upgraded`: A file replaced a worse copy
- `file.renamed`: A file was renamed or moved by the naming
- `file.deleted`: A file was deleted or recycled
- `library.folder_changed`: Files in library folders changed
- `system.started`: Fetchbox started
- `system.stopping`: Fetchbox is shutting down

## Media kinds

### `MediaKind`

`class MediaKind`

A new kind of media (comics, audiobooks, ...), handled like movies: added on the Add
page, searched for on your indexers, downloaded, imported, named and upgraded. Fill in
the class attributes and the methods search, title, parse and name_tokens (queries and
pick have defaults); Fetchbox does the rest.

```python
id: ClassVar[str]
name: ClassVar[str]  # the Add page button and the Library tab: "Comics"
levels: ClassVar[tuple[str, ...]]  # the words the UI uses, title first: ("series", "issue")
file_types: ClassVar[set[str] | frozenset[str]]  # with the dot: {".cbz", ".cbr"}
grab_level: ClassVar[Literal["item", "group"]] = "item"  # music grabs whole albums
qualities: ClassVar[Qualities]
naming: ClassVar[Naming]
settings_model: ClassVar[type[Settings] | None] = None
categories: ClassVar[tuple[int, ...]] = ()  # indexer categories searched by default
settings: Any = None  # the validated settings, set by Fetchbox before any method runs
```

- **`async search(ctx: Context, text: str) -> list[Hit]`**: 1. Titles matching what someone typed on the Add page.
- **`async title(ctx: Context, ref: str) -> TitleDetails`**: 2. A title's groups and items, with numbers, names and release dates.
- **`queries(title: TitleDetails, items: list[Item]) -> list[Query]`**: 3. What to ask indexers for these wanted items. Default: "<title> <number>" each.
- **`parse(release_name: str) -> Parsed | None`**: 4. What a release name says; None when it isn't this kind's.
- **`pick(files: list[File], items: list[Item]) -> dict[str, str]`**: 5. Which file is which item: {file path: item ref}. Default: with one item, the largest file of the kind's types; otherwise numbers in the file names.
- **`name_tokens(title: TitleDetails, item: Item) -> dict[str, object]`**: 6. The values the naming's tokens stand for, for this item's file.

### `Hit`

`class Hit`

One search result on the Add page.

```python
ref: str  # your metadata source's id for it ("4050-49901"); given back to title()
name: str
year: int | None = None
overview: str = ""
poster_url: str | None = None
```

### `TitleDetails`

`class TitleDetails`

A title as the kind's metadata source describes it: its groups and items with their
numbers, names and release dates. Asked for when it's added and again on refresh.

```python
ref: str
name: str
year: int | None = None
overview: str = ""
poster_url: str | None = None
groups: list[Group] = field(default_factory=list)
items: list[Item] = field(default_factory=list)
extra: dict[str, Any] = field(default_factory=dict)  # yours: given back to name_tokens
```

### `Group`

`class Group`

The middle level (a volume, a series of books); kinds with two levels have none.

```python
ref: str
number: float | None = None
name: str | None = None
```

### `Item`

`class Item`

What's wanted and downloaded: an issue, a book, an episode.

```python
ref: str  # stable within its title
number: float | None = None  # 61, 1.5; None when items aren't numbered
name: str | None = None
released: date | None = None  # None: not announced; in the future: not searched yet
group: str | None = None  # the ref of the Group it's in
```

### `Query`

`class Query`

One search to send to indexers.

```python
text: str
categories: tuple[int, ...] = ()  # Newznab/Torznab categories; () = the kind's default
```

### `Parsed`

`class Parsed`

What a release name says (parse() returns None for one that isn't this kind's).

```python
title: str  # the title words, as written: "Saga"
numbers: tuple[float, ...] = ()  # the items it holds: (61,), or (1, 2, 3) for a pack
group: float | None = None  # a volume's number, when it's a whole volume
year: int | None = None
quality: str | None = None  # one of the kind's qualities, when the name says
release_group: str | None = None
```

### `File`

`class File`

A file in a finished download, as pick() sees it.

```python
path: str  # inside the download, with / between folders
size: int
```

### `Qualities`

`class Qualities`

The kind's qualities, best first. Fetchbox's quality profiles for the kind are made
from them, and an upgrade is a file of a better one.

- **`rank(name: str) -> int`**: 0 for the best; ValueError for a quality that isn't the kind's.

### `Naming`

`class Naming`

The default naming for the kind's files, with the tokens it may use. People change
the pattern in Settings; the tokens are what name_tokens() fills in.

### `KindDeclarationError`

`class KindDeclarationError(TypeError)`

A media kind declared wrong; the message says what to change.

## Settings

### `Settings`

`class Settings(BaseModel)`

Base for a plugin's settings. Validated on every save; unknown fields are refused.

```python
model_config = ConfigDict(extra="forbid", validate_assignment=True, validate_default=True)
```

- **`async test(ctx: Context) -> str`**: Optional: the Test button. Return what worked; raise to show what didn't.

### `Field`

```python
Field(default: Any = Ellipsis, *, title: str | None = None, help: str | None = None, ge: float | None = None, le: float | None = None, choices: dict[str, str] | None = None, **kwargs: Any) -> Any
```

A setting: ``title`` is its label, ``help`` the hint under it, ``ge``/``le`` limits.
``choices`` ({value: name}) shows names to pick from instead of a text box; on a list
setting, several in order (Fetchbox 0.10.4 or later).

### `Secret`

`class Secret`

A password or API key: a password field, never sent back to the browser, encrypted
in Fetchbox's database. Read it with ``.value``.

- **`value`** → `str`

### `Url`

```python
Url(*args, **kwargs)
```

Runtime representation of an annotated type.

At its core 'Annotated[t, dec1, dec2, ...]' is an alias for the type 't'
with extra annotations. The alias behaves like a normal typing alias.
Instantiating is the same as instantiating the underlying type; binding
it to types is also the same.

The metadata itself is stored in a '__metadata__' attribute as a tuple.

### `Folder`

```python
Folder(*args, **kwargs)
```

Runtime representation of an annotated type.

At its core 'Annotated[t, dec1, dec2, ...]' is an alias for the type 't'
with extra annotations. The alias behaves like a normal typing alias.
Instantiating is the same as instantiating the underlying type; binding
it to types is also the same.

The metadata itself is stored in a '__metadata__' attribute as a tuple.

## What ctx gives you

### `Context`

`class Context(Protocol)`

```python
plugin_id: str
log: Any  # a structlog logger tagged with the plugin's id: ctx.log.info("synced", n=3)
```

- **`http`** → `Http`
- **`cache`** → `Cache`
- **`store`** → `Store`
- **`folder`** → `Path`
- **`files`** → `Files`
- **`library`** → `Library`
- **`jobs`** → `Jobs`
- **`notify`** → `Notify`

### `ctx: Http`

`class Http(Protocol)`

Fetchbox's timeouts, retries for GETs, a User-Agent naming the plugin, and per-host
rate limits: ``ctx.http.limit("api.example.com", per_second=1)``.

- **`limit(host: str, *, per_second: float) -> None`**
- **`async request(method: str, url: str, **kwargs: Any) -> httpx.Response`**
- **`async get(url: str, **kwargs: Any) -> httpx.Response`**
- **`async post(url: str, **kwargs: Any) -> httpx.Response`**

### `ctx: Cache`

`class Cache(Protocol)`

Answers kept for a while (in memory).

- **`get(key: str, default: Any = None) -> Any`**
- **`set(key: str, value: Any, seconds: float) -> None`**
- **`async remember(key: str, seconds: float, fetch: Callable[[], Awaitable[T]]) -> T`**
- **`clear() -> None`**

### `ctx: Store`

`class Store(Protocol)`

A small key-value store, kept in Fetchbox's database (in backups). JSON values, up to
64 KB each; bigger things go in ``ctx.folder``.

- **`async get(key: str, default: Any = None) -> Any`**
- **`async set(key: str, value: Any) -> None`**
- **`async delete(key: str) -> bool`**
- **`async keys(prefix: str = ) -> list[str]`**

### `ctx: Files`

`class Files(Protocol)`

A library file's place on disk, and sidecar files: written beside a file and from then
on renamed, moved, recycled and deleted with it.

- **`async path(file_id: str) -> Path`**
- **`async sidecars(file_id: str, *, suffix: str | None = None) -> list[SidecarInfo]`**
- **`async add_sidecar(file_id: str, suffix: str, data: bytes, *, meta: dict[str, Any] | None = None) -> SidecarInfo`**
- **`async remove_sidecar(file_id: str, suffix: str) -> bool`**

### `ctx: Library`

`class Library(Protocol)`

Titles and their files, read-only; wanting and searching go through the same rules
and events as the web UI.

- **`async titles(*, kind: str | None = None) -> list[TitleInfo]`**
- **`async title(title_id: str) -> TitleInfo`**
- **`async items(title_id: str) -> list[ItemInfo]`**
- **`async item(item_id: str) -> ItemInfo`**
- **`async files(title_id: str) -> list[FileInfo]`**
- **`async file(file_id: str) -> FileInfo`**
- **`async wanted(item_id: str, monitored: bool = True) -> None`**
- **`async search(item_id: str) -> str | None`**

### `ctx: Jobs`

`class Jobs(Protocol)`

- **`job(name: str, *, max_attempts: int = 3) -> Callable[[Any], Any]`**
- **`async enqueue(name: str, payload: dict[str, Any] | None = None, *, dedupe_key: str | None = None) -> str | None`**

### `ctx: Job`

`class Job(Protocol)`

A running job: its payload, and how it reports progress.

```python
payload: dict[str, Any]
```

- **`async progress(value: float, message: str | None = None) -> None`**
- **`async check_cancelled() -> None`**

### `ctx: Notify`

`class Notify(Protocol)`

### `TitleInfo`

`class TitleInfo`

TitleInfo(id: 'str', kind: 'str', name: 'str', year: 'int | None', ids: 'dict[str, Any]' = <factory>, monitored: 'bool' = True, folder: 'str | None' = None)

```python
id: str
kind: str  # "movie", "series", "music" or a plugin's kind
name: str
year: int | None
ids: dict[str, Any] = field(default_factory=dict)  # tmdb, tvdb, imdb, mbid: those it has
monitored: bool = True
folder: str | None = None  # its own folder in the library folder, once decided
```

### `ItemInfo`

`class ItemInfo`

What's wanted and downloaded: a movie, an episode, an album.

```python
id: str
title_id: str
level: str  # "movie", "episode" or "album"
name: str | None  # the episode's or album's name (a movie's is its title's)
season: int | None  # an episode's season
number: int | None  # an episode's number in its season
released: date | None  # air date, release date
monitored: bool  # wanted: searched for and downloaded
state: str  # missing, searching, grabbed, downloading, importing, acquired
ids: dict[str, Any] = field(default_factory=dict)  # an album's mbid
```

### `FileInfo`

`class FileInfo`

FileInfo(id: 'str', title_id: 'str | None', path: 'Path', size: 'int', quality: 'str', item_ids: 'tuple[str, ...]', imported_at: 'datetime')

```python
id: str
title_id: str | None
path: Path  # where it is now
size: int
quality: str  # "WEBDL-1080p", "FLAC"
item_ids: tuple[str, ...]  # the wanted items (episode, movie, album) it is
imported_at: datetime
```

### `SidecarInfo`

`class SidecarInfo`

SidecarInfo(file_id: 'str', suffix: 'str', path: 'Path', plugin: 'str', meta: 'dict[str, Any]' = <factory>)

```python
file_id: str
suffix: str  # "en.srt"
path: Path
plugin: str  # the plugin that added it
meta: dict[str, Any] = field(default_factory=dict)
```

## Errors

### `NotFound`

`class NotFound(LookupError)`

There's no such title, item or file (any more): ``except NotFound``.

### `Refused`

`class Refused(PermissionError)`

Something a plugin may not do, such as changing another plugin's sidecar.
