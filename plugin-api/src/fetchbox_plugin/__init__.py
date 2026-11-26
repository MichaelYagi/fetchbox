"""The Fetchbox plugin API, version 1.

Everything exported here is the API: within API 1 it only grows; nothing is removed,
renamed or changes meaning. Install ``fetchbox-plugin-api`` to write, type-check and test a
plugin; Fetchbox itself includes it (``fetchbox.plugin`` is the same thing).
"""

from fetchbox_plugin.addon import Addon, Panel, action, every, on, panel
from fetchbox_plugin.context import Context
from fetchbox_plugin.events import PUBLIC_EVENTS, PublicEvent
from fetchbox_plugin.manifest import PERMISSION_WORDS, Permission, Plugin
from fetchbox_plugin.settings import Field, Folder, Secret, Settings, Url
from fetchbox_plugin.types import FileInfo, SidecarInfo, TitleInfo

API_VERSION = 1

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
]
