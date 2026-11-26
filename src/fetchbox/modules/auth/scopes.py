"""Scope catalog and checks.

Scopes are ``resource:action`` with actions ``read`` and ``write``; write implies read.
``*`` grants everything, present and future. ``auth:admin`` is only granted explicitly
or through ``*``.
"""

from __future__ import annotations

from collections.abc import Iterable

ALL = "*"
ADMIN = "auth:admin"

_RESOURCES: dict[str, str] = {
    "media": "Titles, seasons, episodes, wanted items, monitoring, unlock/redownload",
    "discovery": "Metadata search and lookup",
    "releases": "Release search, manual grab, blocklist",
    "downloads": "Queue, pause/resume/remove, priorities, clients",
    "library": "Files, import, rename, maintenance",
    "sources": "Indexers and definitions",
    "storage": "Nodes, destinations, path mappings",
    "profiles": "Quality profiles, custom formats, naming templates",
    "automation": "Schedules, rules, RSS",
    # Spec lists only :write for these two; :read added so read-only dashboards work.
    "integrations": "Media-server connections",
    "notifications": "Notification targets",
    "plugins": "Plugins: install, turn on and off, settings, actions",
    "system": "Health, logs, jobs, backups, updates, config export/import",
}

CATALOG: dict[str, str] = {ALL: "All endpoints, present and future"}
for _res, _desc in _RESOURCES.items():
    CATALOG[f"{_res}:read"] = f"Read: {_desc}"
    CATALOG[f"{_res}:write"] = f"Read and write: {_desc}"
CATALOG["discovery:read"] = "Metadata search and lookup"
CATALOG.pop("discovery:write")  # discovery is read-only
CATALOG["events:read"] = "Event stream subscription"
CATALOG[ADMIN] = "Users and API keys"


def unknown_scopes(scopes: Iterable[str]) -> list[str]:
    return sorted(s for s in scopes if s not in CATALOG)


def has_scope(granted: Iterable[str], needed: str) -> bool:
    g = set(granted)
    if ALL in g or needed in g:
        return True
    if needed.endswith(":read"):
        return needed.removesuffix(":read") + ":write" in g
    return False


def can_grant(granter: Iterable[str], requested: Iterable[str]) -> list[str]:
    """Scopes in `requested` the granter does not hold itself (empty list = allowed)."""
    g = set(granter)
    return sorted(s for s in requested if not (ALL in g if s == ALL else has_scope(g, s)))


def normalize(scopes: Iterable[str]) -> list[str]:
    """Dedupe and drop scopes implied by others (`x:read` under `x:write`, all under `*`)."""
    s = set(scopes)
    if ALL in s:
        return [ALL]
    return sorted(x for x in s if not (x.endswith(":read") and x[:-5] + ":write" in s))
