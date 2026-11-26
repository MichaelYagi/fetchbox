"""What ``ctx`` raises that a plugin may want to catch."""

from __future__ import annotations


class NotFound(LookupError):
    """There's no such title, item or file (any more): ``except NotFound``."""


class Refused(PermissionError):
    """Something a plugin may not do, such as changing another plugin's sidecar."""
