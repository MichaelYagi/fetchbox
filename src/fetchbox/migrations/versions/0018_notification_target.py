"""notification_target: webhook / Discord targets and the events each receives

Revision ID: 0018_notification_target
Revises: 0017_destination_mount
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import fetchbox.core.db

revision: str = "0018_notification_target"
down_revision: str | None = "0017_destination_mount"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_target",
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("last_sent_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_target")),
        sa.UniqueConstraint("name", name=op.f("uq_notification_target_name")),
    )


def downgrade() -> None:
    op.drop_table("notification_target")
