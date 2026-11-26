"""media_file_sidecar: files that belong to a media file (added by plugins)

Revision ID: 0021_media_file_sidecar
Revises: 0020_title_folder
Create Date: 2026-11-25 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import fetchbox.core.db

revision: str = "0021_media_file_sidecar"
down_revision: str | None = "0020_title_folder"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_file_sidecar",
        sa.Column("media_file_id", sa.String(length=26), nullable=False),
        sa.Column("suffix", sa.String(length=64), nullable=False),
        sa.Column("plugin", sa.String(length=64), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("created_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["media_file_id"],
            ["media_file.id"],
            name=op.f("fk_media_file_sidecar_media_file_id_media_file"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_file_sidecar")),
        sa.UniqueConstraint(
            "media_file_id", "suffix", name="uq_media_file_sidecar_media_file_id_suffix"
        ),
    )


def downgrade() -> None:
    op.drop_table("media_file_sidecar")
