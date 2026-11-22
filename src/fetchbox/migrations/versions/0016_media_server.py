"""media_server: Plex / Jellyfin / Emby connections

Revision ID: 0016_media_server
Revises: 0015_minimum_availability
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import fetchbox.core.db

revision: str = "0016_media_server"
down_revision: str | None = "0015_minimum_availability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_server",
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("path_mappings", sa.JSON(), nullable=False),
        sa.Column("last_refresh_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_server")),
        sa.UniqueConstraint("name", name=op.f("uq_media_server_name")),
    )


def downgrade() -> None:
    op.drop_table("media_server")
