"""The Plugin class: everything Fetchbox needs to know about a plugin, declared on one class,
so ``fetchbox plugin check`` can read it without running the plugin.

    class Comics(Plugin):
        id = "comics"
        name = "Comics"
        description = "Comic series and issues from ComicVine, filed as CBZ."
        fetchbox_api = 1
        permissions = {Permission.NETWORK, Permission.LIBRARY_FILES}
        contributes = [ComicsKind, Komga]
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar


class Permission(StrEnum):
    """What a plugin uses, shown in plain words before it's installed. Declared, not enforced
    (API 1 has no sandbox); ``fetchbox plugin check`` compares them with the code."""

    NETWORK = "network"  # "Uses the network"
    LIBRARY_FILES = "library_files"  # "Reads and writes files in your library folders"
    LIBRARY = "library"  # "Changes what's wanted and searches for it"
    NOTIFY = "notify"  # "Sends messages through your notification targets"


PERMISSION_WORDS: dict[Permission, str] = {
    Permission.NETWORK: "Uses the network",
    Permission.LIBRARY_FILES: "Reads and writes files in your library folders",
    Permission.LIBRARY: "Changes what's wanted and searches for it",
    Permission.NOTIFY: "Sends messages through your notification targets",
}


class Plugin:
    """Subclass once per plugin package and point the ``fetchbox.plugins`` entry point at it."""

    id: ClassVar[str]  # permanent: settings, stored data and URLs hang off it
    name: ClassVar[str]
    description: ClassVar[str] = ""
    fetchbox_api: ClassVar[int] = 1  # the plugin API version it was written for
    min_fetchbox: ClassVar[str | None] = None  # when it needs an API 1 addition
    homepage: ClassVar[str | None] = None
    version: ClassVar[str | None] = None  # from the package's metadata when not set
    permissions: ClassVar[frozenset[Permission] | set[Permission]] = frozenset()
    contributes: ClassVar[list[type[Any]]] = []  # media kinds, add-ons and providers
