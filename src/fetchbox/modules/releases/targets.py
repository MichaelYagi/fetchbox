"""Build the decision engine's ``Target`` from what the library stores."""

from __future__ import annotations

from fetchbox.modules.media.models import Title
from fetchbox.modules.releases.decision import Existing, Target


def movie_target(title: Title, existing: Existing | None = None) -> Target:
    """Names to match: the display name, the original title, then TMDB's alternates."""
    if title.kind != "movie":
        raise ValueError(f"title {title.id} is a {title.kind}, not a movie")
    meta = title.meta or {}
    names = [title.name, meta.get("original_name"), *(meta.get("alt_titles") or [])]
    unique = tuple(dict.fromkeys(n for n in names if n))
    return Target(
        "movie",
        unique,
        year=title.year,
        original_language=meta.get("original_language"),
        runtime_minutes=meta.get("runtime"),
        existing=existing,
    )
