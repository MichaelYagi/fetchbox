"""Public events: what happens in Fetchbox that a plugin can react to (``@on("file.imported")``).

Within API 1 these names and their fields keep their meaning; new events may be added.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# event type -> what it means
PUBLIC_EVENTS: dict[str, str] = {
    "title.added": "A title was added",
    "title.updated": "A title's details or settings changed",
    "title.removed": "A title was removed (its files stay on disk)",
    "release.grabbed": "A release was sent to a download client",
    "release.blocklisted": "A release was blocklisted",
    "download.completed": "A download finished",
    "download.failed": "A download failed",
    "file.imported": "Files were filed into the library (an album: every track)",
    "file.upgraded": "A file replaced a worse copy",
    "file.renamed": "A file was renamed or moved by the naming",
    "file.deleted": "A file was deleted or recycled",
    "library.folder_changed": "Files in library folders changed",
    "system.started": "Fetchbox started",
    "system.stopping": "Fetchbox is shutting down",
}


class PublicEvent(BaseModel):
    """One event as plugins see it."""

    id: str
    type: str
    occurred_at: datetime
    title_id: str | None = None
    kind: str | None = Field(default=None, description="The title's media kind")
    file_ids: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def file(self) -> str | None:
        """The event's file, when it's about one (``file.imported`` of a movie)."""
        return self.file_ids[0] if self.file_ids else None
