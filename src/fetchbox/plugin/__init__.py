"""Fetchbox's plugin API (version 1, in progress): what plugins may use and rely on.

Everything exported here is public and stable within an API version; anything imported
from elsewhere in ``fetchbox`` is internal and may change in any release.
"""

from fetchbox.plugin.events import PUBLIC_EVENTS, PublicEvent, public_events, to_public

API_VERSION = 1

__all__ = ["API_VERSION", "PUBLIC_EVENTS", "PublicEvent", "public_events", "to_public"]
