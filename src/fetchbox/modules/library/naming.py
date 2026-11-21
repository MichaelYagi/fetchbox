"""Naming templates: ``{Token}`` placeholders and ``[...]`` conditional segments.

* ``{Title}`` inserts a token; ``{Season:00}`` zero-pads numbers to the given width.
* ``[ ... ]`` is a conditional segment: it disappears when any token inside it is
  empty, and its brackets are not output (``S{Season:00}E{Episode:00}[-E{EpisodeEnd:00}]``
  gives ``S01E01`` or ``S01E01-E02``).
* ``{{`` / ``}}`` / ``[[`` / ``]]`` are literal ``{`` ``}`` ``[`` ``]``.

After rendering, runs of spaces collapse and dangling separators at the end go, so an
empty trailing token doesn't leave ``Movie -``. Every path component is then made safe
for Windows (and so for SMB shares and any OS): illegal characters replaced, trailing
dots and spaces removed, reserved device names suffixed, length capped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PATTERN_KEYS = ("movie_folder", "movie_file", "series_folder", "season_folder", "episode_file")

# Earlier default episode pattern: installs still using it unchanged get the new one.
OLD_EPISODE_FILE = (
    "{Title} - S{Season:00}E{Episode:00}[-E{EpisodeEnd:00}] - {EpisodeTitle} [{Quality}]"
)
DEFAULT_PATTERNS: dict[str, str] = {
    "movie_folder": "{Title} ({Year}) {{tmdb-{TmdbId}}}",
    "movie_file": "{Title} ({Year}) [{Edition}] - {Quality} [{HDR}] [{Codec}]-{Group}",
    "series_folder": "{Title} ({Year}) {{tvdb-{TvdbId}}}",
    "season_folder": "Season {Season:00}",
    "episode_file": (
        "{Title} - S{Season:00}E{Episode:00}[-E{EpisodeEnd:00}][ - {EpisodeTitle}][ [[{Quality}]]]"
    ),
}

TOKENS: dict[str, str] = {
    "Title": "Movie or series title",
    "Year": "Release year",
    "TmdbId": "TMDB id",
    "ImdbId": "IMDb id (tt...)",
    "TvdbId": "TVDB id",
    "Edition": "Edition, e.g. Director's Cut",
    "Quality": "Quality name, e.g. WEBDL-1080p",
    "Resolution": "e.g. 1080p",
    "Source": "e.g. WEB-DL, BluRay",
    "HDR": "e.g. DV HDR10",
    "Codec": "e.g. x265",
    "Audio": "e.g. DDP 5.1 Atmos",
    "Group": "Release group",
    "Proper": "PROPER / REPACK when the release is one",
    "OriginalName": "The release name as grabbed",
    "Season": "Season number",
    "Episode": "Episode number",
    "EpisodeEnd": "Last episode of a multi-episode file",
    "EpisodeTitle": "Episode title",
    "AirDate": "Air date, YYYY-MM-DD",
}

_WIN_ILLEGAL = re.compile(r'[<>"|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?$", re.I)
MAX_COMPONENT = 200  # characters; leaves room for extensions and partial suffixes


class TemplateError(ValueError):
    pass


@dataclass(frozen=True)
class _Lit:
    text: str


@dataclass(frozen=True)
class _Tok:
    name: str
    pad: int | None


@dataclass(frozen=True)
class _Opt:
    parts: tuple[_Lit | _Tok, ...]


Part = _Lit | _Tok | _Opt


def parse(template: str) -> list[Part]:
    """Parse a template; raises TemplateError on unknown tokens or unbalanced brackets."""
    out: list[Part] = []
    stack: list[list[_Lit | _Tok]] = []
    i, n = 0, len(template)

    def emit(part: _Lit | _Tok) -> None:
        (stack[-1] if stack else out).append(part)

    while i < n:
        two = template[i : i + 2]
        ch = template[i]
        if two in ("{{", "}}", "[[", "]]"):
            emit(_Lit(ch))
            i += 2
        elif ch == "{":
            end = template.find("}", i)
            if end < 0:
                raise TemplateError(f"unclosed {{ at position {i}")
            body = template[i + 1 : end]
            name, _, fmt = body.partition(":")
            if name not in TOKENS:
                raise TemplateError(f"unknown token {{{name}}}; known: {', '.join(TOKENS)}")
            if fmt and not re.fullmatch("0+", fmt):
                raise TemplateError(f"unsupported format {{{body}}}: use zeros, e.g. {{{name}:00}}")
            emit(_Tok(name, len(fmt) if fmt else None))
            i = end + 1
        elif ch == "}":
            raise TemplateError(f"unmatched }} at position {i} (write }}}} for a literal brace)")
        elif ch == "[":
            if stack:
                raise TemplateError("conditional segments [ ] cannot be nested")
            stack.append([])
            i += 1
        elif ch == "]":
            if not stack:
                raise TemplateError(f"unmatched ] at position {i} (write ]] for a literal one)")
            out.append(_Opt(tuple(stack.pop())))
            i += 1
        else:
            j = i
            while j < n and template[j] not in "{}[]":
                j += 1
            emit(_Lit(template[i:j]))
            i = j
    if stack:
        raise TemplateError("unclosed [ conditional segment")
    return out


def _value(tok: _Tok, ctx: Mapping[str, Any]) -> str:
    v = ctx.get(tok.name)
    if v is None or v == "" or v == []:
        return ""
    if isinstance(v, list | tuple):
        v = " ".join(str(x) for x in v if x)
    if tok.pad is not None and isinstance(v, int):
        return str(v).zfill(tok.pad)
    return str(v)


def render(template: str | list[Part], ctx: Mapping[str, Any]) -> str:
    parts = parse(template) if isinstance(template, str) else template
    out: list[str] = []
    for p in parts:
        if isinstance(p, _Lit):
            out.append(p.text)
        elif isinstance(p, _Tok):
            out.append(_value(p, ctx))
        else:
            values = [(_value(x, ctx) if isinstance(x, _Tok) else x.text) for x in p.parts]
            tokens = [x for x in p.parts if isinstance(x, _Tok)]
            if all(_value(t, ctx) for t in tokens):
                out.append("".join(values))
    text = re.sub(r"\s+", " ", "".join(out))
    text = re.sub(r"\(\s*\)|\{\s*\}", "", text)  # "()" / "{}" left by an empty token
    text = re.sub(r"\s+", " ", text)
    return text.strip().rstrip(" -_.").strip()


def safe_component(name: str) -> str:
    """Make one path component valid on Windows (and therefore everywhere)."""
    s = name.replace(":", " -").replace("/", "-").replace("\\", "-")
    s = _WIN_ILLEGAL.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(" .")
    if len(s) > MAX_COMPONENT:
        s = s[:MAX_COMPONENT].rstrip(" .")
    if _RESERVED.match(s):
        s += "_"
    return s or "_"


def validate_patterns(patterns: Mapping[str, str]) -> dict[str, str]:
    unknown = set(patterns) - set(PATTERN_KEYS)
    if unknown:
        raise TemplateError(
            f"unknown pattern(s) {sorted(unknown)}; known: {', '.join(PATTERN_KEYS)}"
        )
    out = {**DEFAULT_PATTERNS, **patterns}
    for key, tpl in out.items():
        try:
            parse(tpl)
        except TemplateError as exc:
            raise TemplateError(f"{key}: {exc}") from None
        if not tpl.strip():
            raise TemplateError(f"{key}: must not be empty")
    return out


def movie_path(patterns: Mapping[str, str], ctx: Mapping[str, Any], extension: str) -> str:
    """Relative path ``<folder>/<file>.<ext>`` for a movie, with safe components."""
    folder = safe_component(render(patterns["movie_folder"], ctx))
    stem = safe_component(render(patterns["movie_file"], ctx))
    ext = extension.lower().lstrip(".")
    return f"{folder}/{stem}.{ext}" if ext else f"{folder}/{stem}"


def episode_path(patterns: Mapping[str, str], ctx: Mapping[str, Any], extension: str) -> str:
    """Relative path ``<series>/<season>/<file>.<ext>`` for an episode file. An empty
    season folder pattern puts episodes straight into the series folder."""
    series = safe_component(render(patterns["series_folder"], ctx))
    season = render(patterns["season_folder"], ctx).strip() if patterns.get("season_folder") else ""
    stem = safe_component(render(patterns["episode_file"], ctx))
    ext = extension.lower().lstrip(".")
    name = f"{stem}.{ext}" if ext else stem
    return f"{series}/{safe_component(season)}/{name}" if season else f"{series}/{name}"
