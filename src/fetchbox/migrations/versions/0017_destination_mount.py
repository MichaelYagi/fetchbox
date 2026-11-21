"""destination.mount_point: the mount a library folder lives on, to detect it unmounted

Revision ID: 0017_destination_mount
Revises: 0016_media_server
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_destination_mount"
down_revision: str | None = "0016_media_server"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("destination", schema=None) as batch_op:
        batch_op.add_column(sa.Column("mount_point", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("destination", schema=None) as batch_op:
        batch_op.drop_column("mount_point")
