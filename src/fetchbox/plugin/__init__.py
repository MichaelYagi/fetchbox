"""Fetchbox's plugin API (version 1): the plugin API package, ``fetchbox_plugin``, plus the
parts that only exist inside Fetchbox (translating its events, the plugin context).

``from fetchbox.plugin import Plugin`` and ``from fetchbox_plugin import Plugin`` are the
same; plugins use ``fetchbox_plugin`` so they need only ``fetchbox-plugin-api``.
"""

from fetchbox.plugin.events import public_events, to_public
from fetchbox_plugin import (
    API_VERSION,
    PERMISSION_WORDS,
    PUBLIC_EVENTS,
    Addon,
    Context,
    Field,
    FileInfo,
    Folder,
    Panel,
    Permission,
    Plugin,
    PublicEvent,
    Secret,
    Settings,
    SidecarInfo,
    TitleInfo,
    Url,
    action,
    every,
    on,
    panel,
)

__all__ = [
    "API_VERSION",
    "PERMISSION_WORDS",
    "PUBLIC_EVENTS",
    "Addon",
    "Context",
    "Field",
    "FileInfo",
    "Folder",
    "Panel",
    "Permission",
    "Plugin",
    "PublicEvent",
    "Secret",
    "Settings",
    "SidecarInfo",
    "TitleInfo",
    "Url",
    "action",
    "every",
    "on",
    "panel",
    "public_events",
    "to_public",
]
