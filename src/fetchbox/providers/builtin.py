"""Importing this registers every built-in provider with ``core.providers.registry``."""

from __future__ import annotations

import fetchbox.providers.download_clients.builtin
import fetchbox.providers.download_clients.direct
import fetchbox.providers.download_clients.external
import fetchbox.providers.indexers.definition
import fetchbox.providers.indexers.prowlarr
import fetchbox.providers.indexers.torznab
import fetchbox.providers.metadata.tmdb
import fetchbox.providers.storage.local  # noqa: F401
