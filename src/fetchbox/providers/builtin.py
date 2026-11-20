"""Importing this registers every built-in provider with ``core.providers.registry``."""

from __future__ import annotations

import fetchbox.providers.indexers.torznab
import fetchbox.providers.metadata.tmdb  # noqa: F401
