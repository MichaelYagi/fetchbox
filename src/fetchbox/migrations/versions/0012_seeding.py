"""download.seeded_at: when the seeding goal was met and the torrent stopped

Revision ID: 0012_seeding
Revises: 0011_library
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fetchbox.core.db import UTCDateTime

revision: str = "0012_seeding"
down_revision: str | None = "0011_library"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("download", schema=None) as batch_op:
        batch_op.add_column(sa.Column("seeded_at", UTCDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("download", schema=None) as batch_op:
        batch_op.drop_column("seeded_at")
