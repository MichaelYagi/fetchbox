"""Series monitoring: which seasons and episodes a monitor mode wants.

Six modes, per the spec:

- ``all``: every episode of every regular season.
- ``future``: episodes that haven't aired yet when the series is added, and every
  episode that appears later.
- ``missing``: episodes without a file (at add time, all of them), and new ones.
- ``first_season``: season 1 only.
- ``latest_season``: the newest regular season; when a new season starts, it becomes the
  monitored one (the old one keeps whatever it had).
- ``none``: nothing.

Specials (season 0) are never monitored by a mode; monitor them per season.

``initial`` decides at add time (or when the mode changes); ``for_new`` decides for an
episode a refresh discovers later.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal

SeriesMode = Literal["all", "future", "missing", "first_season", "latest_season", "none"]
SERIES_MODES: tuple[SeriesMode, ...] = (
    "all", "future", "missing", "first_season", "latest_season", "none",
)  # fmt: skip


@dataclass(frozen=True)
class Ep:
    season: int
    number: int
    air_date: date | None
    has_file: bool = False


def aired(air_date: date | None, today: date) -> bool:
    return air_date is not None and air_date <= today


def latest_season(seasons: Iterable[int]) -> int | None:
    regular = [s for s in seasons if s > 0]
    return max(regular) if regular else None


def initial(mode: SeriesMode, episodes: Iterable[Ep], today: date) -> dict[tuple[int, int], bool]:
    """Monitored flag per (season, episode) for a whole series."""
    eps = list(episodes)
    latest = latest_season(e.season for e in eps)
    out: dict[tuple[int, int], bool] = {}
    for e in eps:
        if e.season == 0 or mode == "none":
            on = False
        elif mode == "all":
            on = True
        elif mode == "future":
            on = not aired(e.air_date, today)
        elif mode == "missing":
            on = not e.has_file
        elif mode == "first_season":
            on = e.season == 1
        else:  # latest_season
            on = e.season == latest
        out[(e.season, e.number)] = on
    return out


def seasons_monitored(mode: SeriesMode, flags: dict[tuple[int, int], bool]) -> dict[int, bool]:
    """A season is monitored when any of its episodes is (or, for ``all``-like modes, when
    it is a regular season), so a new episode in it is picked up."""
    out: dict[int, bool] = {}
    for (season, _), on in flags.items():
        out[season] = out.get(season, False) or on
    if mode in ("all", "future", "missing"):
        for season in out:
            out[season] = out[season] or season > 0
    return out


def for_new(
    mode: SeriesMode, season: int, season_monitored: bool | None, latest: int | None
) -> bool:
    """A newly listed episode. ``season_monitored`` is None for a season new to us;
    ``latest`` is the newest regular season including this one."""
    if season == 0 or mode == "none":
        return bool(season_monitored) if season == 0 else False
    if mode in ("all", "future", "missing"):
        return season_monitored is not False
    if mode == "first_season":
        return season == 1 and season_monitored is not False
    # latest_season: a new season takes over; within a known season, follow the season
    if season_monitored is None:
        return season == latest
    return season_monitored
