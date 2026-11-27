"""Subtitles: subtitles from OpenSubtitles for movies and episodes, filed beside them.

When a movie or an episode is imported (or upgraded), the subtitles in your languages are
fetched and written beside it as ``<its name>.<language>.srt``. Fetchbox keeps them with the
file from then on: renamed, moved and deleted together with it.

A language not found yet is tried again once a day; "Search subtitles" on a movie or episode
tries at once.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, ClassVar

from fetchbox_plugin import (
    Addon,
    Field,
    FileInfo,
    ItemInfo,
    NotFound,
    Panel,
    Permission,
    Plugin,
    PublicEvent,
    Secret,
    Settings,
    action,
    every,
    on,
    panel,
)
from fetchbox_subtitles.languages import LANGUAGES
from fetchbox_subtitles.moviehash import moviehash
from fetchbox_subtitles.opensubtitles import Found, OpenSubtitles, OpenSubtitlesError, QuotaReached

MISSING = "missing"  # store: {file id: [languages not found yet]}
PAUSED = "paused_until"  # store: when the daily downloads run out, until when
RETRY_PER_DAY = 40  # files tried again a day (OpenSubtitles' free downloads are limited)


class SubtitleSettings(Settings):
    api_key: Secret = Field(
        title="OpenSubtitles API key",
        help="Free: sign up at opensubtitles.com, then API Consumers in your profile",
    )
    languages: list[str] = Field(
        ["en"],
        title="Languages",
        help="Each one gets its own subtitle file; the first comes first",
        choices=LANGUAGES,
    )
    username: str = Field("", title="OpenSubtitles username", help="Optional: more downloads a day")
    password: Secret = Field("", title="OpenSubtitles password")
    hearing_impaired: bool = Field(False, title="Prefer hearing impaired subtitles")

    async def test(self, ctx: Any) -> str:
        """The Test button: the key (and the account, when given) work."""
        return await OpenSubtitles(
            ctx, self.api_key.value, self.username, self.password.value
        ).check()


def pick(found: list[Found], *, hearing_impaired: bool) -> Found | None:
    """The best: timed to this exact file, made by people, as asked about hearing impaired,
    then the most downloaded."""
    if not found:
        return None
    return max(
        found,
        key=lambda f: (
            f.hash_match,
            not f.machine_made,
            f.hearing_impaired == hearing_impaired,
            f.downloads,
        ),
    )


class Subtitles(Addon):
    id = "subtitles"
    name = "Subtitles"
    applies_to: ClassVar[set[str]] = {"movie", "series"}
    settings = SubtitleSettings

    def _client(self, ctx: Any) -> OpenSubtitles:
        s: SubtitleSettings = self.settings
        return OpenSubtitles(ctx, s.api_key.value, s.username, s.password.value)

    @on("file.imported", "file.upgraded")
    async def fetch_new(self, ctx: Any, event: PublicEvent) -> dict[str, Any]:
        got = 0
        for file_id in event.file_ids:
            got += await self.fetch(ctx, file_id)
        return {"subtitles": got}

    @action("Search subtitles", on="item", needs_file=True)
    async def search_now(self, ctx: Any, item: str) -> dict[str, Any]:
        it = await ctx.library.item(item)
        got, files = 0, []
        for f in await ctx.library.files(it.title_id):
            if item in f.item_ids:
                files.append(f.id)
                got += await self.fetch(ctx, f.id)
        waiting = await ctx.store.get(MISSING, {})
        if not files:
            message = "No file yet: subtitles come once it's downloaded"
        elif got:
            message = f"{got} subtitle(s) added"
        elif any(f in waiting for f in files):
            message = "None found yet; tried again daily"
        else:
            message = "It has subtitles in all your languages"
        return {"subtitles": got, "message": message}

    @action("Find missing subtitles", on="title", needs_file=True)
    async def find_for_title(self, ctx: Any, title: str) -> dict[str, Any]:
        """Every file of a movie or show without subtitles in one of your languages."""
        return await self._find(ctx, [title])

    @action("Find missing subtitles", on="group", needs_file=True)
    async def find_for_season(self, ctx: Any, group: str) -> dict[str, Any]:
        """One season of a show ("<title id>:<season>")."""
        title_id, _, season = group.rpartition(":")
        want = int(season) if season.isdigit() else None
        files = []
        for f in await ctx.library.files(title_id):
            seasons = {(await ctx.library.item(i)).season for i in f.item_ids}
            if want in seasons:
                files.append(f)
        return await self._check(ctx, files)

    @action("Find missing subtitles", on="library")
    async def find_everywhere(self, ctx: Any) -> dict[str, Any]:
        """The same for the whole library: the movies and shows already there."""
        titles = [t.id for k in ("movie", "series") for t in await ctx.library.titles(kind=k)]
        return await self._find(ctx, titles)

    async def _find(self, ctx: Any, title_ids: list[str]) -> dict[str, Any]:
        files = [f for tid in title_ids for f in await ctx.library.files(tid)]
        return await self._check(ctx, files)

    async def _check(self, ctx: Any, files_in: list[Any]) -> dict[str, Any]:
        """These files, those lacking a language: noted (the daily retry works through
        them), a first batch fetched now, and a message saying how it went. Only these files
        are fetched now; others already waiting keep waiting their turn."""
        missing: dict[str, list[str]] = await ctx.store.get(MISSING, {})
        files, lacking = 0, []
        for f in files_in:
            files += 1
            have = {s.suffix for s in await ctx.files.sidecars(f.id)}
            if lack := [x for x in self.settings.languages if f"{x}.srt" not in have]:
                missing[f.id] = sorted(set(missing.get(f.id, [])) | set(lack))
                lacking.append(f.id)
        await ctx.store.set(MISSING, missing)
        got = 0
        for file_id in lacking[:RETRY_PER_DAY]:
            got += await self.fetch(ctx, file_id)
        waiting = await ctx.store.get(MISSING, {})
        left = sum(1 for f in lacking if f in waiting)
        if not lacking:
            said = (f"{files} file(s) checked: all have subtitles in your languages." if files
                    else "No movie or episode files to check yet.")  # fmt: skip
            return {"message": said,
                    "checked": files, "queued": 0, "added": 0, "left": 0}  # fmt: skip
        if left and await self._paused(ctx):
            later = f"OpenSubtitles' downloads for today are used up; {left} continue tomorrow"
        elif left:
            later = f"{left} still to find, tried again daily ({RETRY_PER_DAY} files a day)"
        else:
            later = "nothing left to find"
        message = (
            f"{files} file(s) checked, {len(lacking)} missing subtitles; {got} added now; {later}."
        )
        return {"message": message, "checked": files, "queued": len(lacking), "added": got,
                "left": left}  # fmt: skip

    @panel(on="title", title="Subtitles")
    async def status(self, ctx: Any, title: str) -> Panel:
        """On a movie's or show's page: each file, and each of your languages: added,
        waiting (tried again daily), or not tried yet."""
        files = await ctx.library.files(title)
        langs = list(self.settings.languages)
        if not files:
            return Panel(status="No file yet: subtitles come once it's downloaded")
        waiting: dict[str, list[str]] = await ctx.store.get(MISSING, {})
        rows, have_count = [], dict.fromkeys(langs, 0)
        for f in files:
            have = {s.suffix for s in await ctx.files.sidecars(f.id)}
            item = await ctx.library.item(f.item_ids[0]) if f.item_ids else None
            name = (
                f"S{item.season:02}E{int(item.number or 0):02}"
                if item is not None and item.level == "episode" and item.season is not None
                else f.path.name
            )
            row: dict[str, Any] = {"File": name}
            for lang in langs:
                if f"{lang}.srt" in have:
                    row[LANGUAGES.get(lang, lang)] = "✓"
                    have_count[lang] += 1
                elif lang in waiting.get(f.id, []):
                    row[LANGUAGES.get(lang, lang)] = "waiting (tried daily)"
                else:
                    row[LANGUAGES.get(lang, lang)] = "—"
            rows.append(row)
        n = len(files)
        status = " · ".join(f"{LANGUAGES.get(x, x)}: {have_count[x]} of {n}" for x in langs)
        if any(f.id in waiting for f in files):
            status += " · the rest tried again daily"
        if await self._paused(ctx):
            status += " · OpenSubtitles' downloads for today are used up"
        return Panel(status=status, table=rows if n > 1 or len(langs) > 1 else [])

    @every(hours=24)
    async def retry_missing(self, ctx: Any) -> dict[str, Any]:
        missing: dict[str, list[str]] = await ctx.store.get(MISSING, {})
        got = 0
        for file_id in list(missing)[:RETRY_PER_DAY]:
            got += await self.fetch(ctx, file_id)
        return {"subtitles": got, "still_missing": len(await ctx.store.get(MISSING, {}))}

    async def fetch(self, ctx: Any, file_id: str) -> int:
        """Subtitles for one file, in each language it doesn't have yet; how many were added."""
        if await self._paused(ctx):
            return 0
        try:
            f: FileInfo = await ctx.library.file(file_id)
        except NotFound:
            await self._forget(ctx, file_id)  # gone from the library
            return 0
        have = {s.suffix for s in await ctx.files.sidecars(file_id)}
        wanted = [lang for lang in self.settings.languages if f"{lang}.srt" not in have]
        if not wanted:
            await self._forget(ctx, file_id)
            return 0
        query = await self._query(ctx, f)
        if query is None:
            ctx.log.info("no ids to search by", file=file_id)
            return 0
        client = self._client(ctx)
        added, still = 0, []
        for lang in wanted:
            try:
                best = pick(
                    await client.search({**query, "languages": lang}),
                    hearing_impaired=self.settings.hearing_impaired,
                )
                if best is None:
                    still.append(lang)
                    continue
                data = await client.download(best.file_id)
            except QuotaReached as exc:
                await ctx.store.set(PAUSED, exc.reset)
                ctx.log.warning("daily downloads used up", until=exc.reset)
                still += [w for w in wanted if w not in still and f"{w}.srt" not in have]
                break
            except OpenSubtitlesError as exc:
                ctx.log.warning("opensubtitles", error=str(exc))
                still.append(lang)
                continue
            await ctx.files.add_sidecar(
                file_id,
                f"{lang}.srt",
                data,
                meta={
                    "source": "OpenSubtitles",
                    "release": best.release,
                    "file_id": best.file_id,
                    "hash_match": best.hash_match,
                },
            )
            added += 1
        await self._remember(ctx, file_id, sorted(set(still)))
        return added

    async def _query(self, ctx: Any, f: FileInfo) -> dict[str, Any] | None:
        """What to search by: the file's hash, and the title's ids (an episode: the show's,
        with its season and number)."""
        if f.title_id is None:
            return None
        title = await ctx.library.title(f.title_id)
        ids: dict[str, Any] = {}
        tmdb, imdb = title.ids.get("tmdb_id"), title.ids.get("imdb_id")
        item: ItemInfo | None = await ctx.library.item(f.item_ids[0]) if f.item_ids else None
        if item is not None and item.level == "episode":
            if tmdb:
                ids["parent_tmdb_id"] = tmdb
            elif imdb:
                ids["parent_imdb_id"] = str(imdb).removeprefix("tt")
            if item.season is None or item.number is None:
                return None
            ids |= {"season_number": item.season, "episode_number": item.number}
        elif tmdb:
            ids["tmdb_id"] = tmdb
        elif imdb:
            ids["imdb_id"] = str(imdb).removeprefix("tt")
        if not ({"tmdb_id", "imdb_id", "parent_tmdb_id", "parent_imdb_id"} & set(ids)):
            return None
        if digest := await asyncio.to_thread(moviehash, f.path):
            ids["moviehash"] = digest
        return ids

    async def _paused(self, ctx: Any) -> bool:
        until = await ctx.store.get(PAUSED)
        if not until:
            return False
        try:
            when = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is not None and when <= datetime.now(UTC):
            await ctx.store.delete(PAUSED)
            return False
        return True

    async def _remember(self, ctx: Any, file_id: str, langs: list[str]) -> None:
        missing: dict[str, list[str]] = await ctx.store.get(MISSING, {})
        if langs:
            missing[file_id] = langs
        else:
            missing.pop(file_id, None)
        await ctx.store.set(MISSING, missing)

    async def _forget(self, ctx: Any, file_id: str) -> None:
        await self._remember(ctx, file_id, [])


class SubtitlesPlugin(Plugin):
    id = "subtitles"
    name = "Subtitles"
    description = "Subtitles from OpenSubtitles for movies and episodes, filed beside them."
    fetchbox_api = 1
    min_fetchbox = "0.10.4"  # the library-wide action and the language choices
    homepage = "https://github.com/MichaelYagi/fetchbox/tree/main/plugins/fetchbox-subtitles"
    permissions: ClassVar[set[Permission]] = {Permission.NETWORK, Permission.LIBRARY_FILES}
    contributes: ClassVar[list[type[Any]]] = [Subtitles]
