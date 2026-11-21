"""download clients and downloads

Revision ID: 0010_downloads
Revises: 0009_grabs
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fetchbox.core.db import UTCDateTime

revision: str = "0010_downloads"
down_revision: str | None = "0009_grabs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "download_client",
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("media_kinds", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_download_client")),
        sa.UniqueConstraint("name", name=op.f("uq_download_client_name")),
    )
    op.create_table(
        "download",
        sa.Column("grab_id", sa.String(length=26), nullable=False),
        sa.Column("client_id", sa.String(length=26), nullable=False),
        sa.Column("client_ref", sa.String(length=64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_kind", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("queue_priority", sa.Integer(), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("save_path", sa.Text(), nullable=True),
        sa.Column("ratio", sa.Float(), nullable=True),
        sa.Column("seed_seconds", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_progress_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=True),
        sa.Column("completed_at", UTCDateTime(), nullable=True),
        sa.Column("removed_at", UTCDateTime(), nullable=True),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'downloading', 'paused', 'completed', 'failed', 'removed')",
            name=op.f("ck_download_status"),
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["download_client.id"],
            name=op.f("fk_download_client_id_download_client"),
        ),
        sa.ForeignKeyConstraint(["grab_id"], ["grab.id"], name=op.f("fk_download_grab_id_grab")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_download")),
        sa.UniqueConstraint("client_id", "client_ref", name="uq_download_client_id_client_ref"),
    )
    with op.batch_alter_table("download", schema=None) as batch_op:
        batch_op.create_index("ix_download_grab_id", ["grab_id"], unique=False)
        batch_op.create_index("ix_download_status", ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("download", schema=None) as batch_op:
        batch_op.drop_index("ix_download_status")
        batch_op.drop_index("ix_download_grab_id")

    op.drop_table("download")
    op.drop_table("download_client")
