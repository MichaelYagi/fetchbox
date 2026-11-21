"""storage nodes, destinations, naming templates, media files; import tracking

Revision ID: 0011_library
Revises: 0010_downloads
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fetchbox.core.db import UTCDateTime

revision: str = "0011_library"
down_revision: str | None = "0010_downloads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "naming_template",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("patterns", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_naming_template")),
        sa.UniqueConstraint("name", name=op.f("uq_naming_template_name")),
    )
    op.create_table(
        "storage_node",
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_storage_node")),
        sa.UniqueConstraint("name", name=op.f("uq_storage_node_name")),
    )
    op.create_table(
        "destination",
        sa.Column("node_id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("media_kind", sa.String(length=10), nullable=False),
        sa.Column("naming_template_id", sa.String(length=26), nullable=True),
        sa.Column("link_mode", sa.String(length=20), nullable=False),
        sa.Column("free_space_min", sa.BigInteger(), nullable=True),
        sa.Column("recycle", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column("checked_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "link_mode IN ('hardlink_or_copy', 'hardlink_only', 'copy', 'move', 'symlink')",
            name=op.f("ck_destination_link_mode"),
        ),
        sa.CheckConstraint(
            "media_kind IN ('movie', 'series')", name=op.f("ck_destination_media_kind")
        ),
        sa.ForeignKeyConstraint(
            ["naming_template_id"],
            ["naming_template.id"],
            name=op.f("fk_destination_naming_template_id_naming_template"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["storage_node.id"], name=op.f("fk_destination_node_id_storage_node")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_destination")),
        sa.UniqueConstraint("name", name=op.f("uq_destination_name")),
    )
    op.create_table(
        "media_file",
        sa.Column("storage_node_id", sa.String(length=26), nullable=False),
        sa.Column("destination_id", sa.String(length=26), nullable=True),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime", UTCDateTime(), nullable=True),
        sa.Column("quick_hash", sa.String(length=64), nullable=True),
        sa.Column("quality", sa.JSON(), nullable=False),
        sa.Column("custom_format_score", sa.Integer(), nullable=False),
        sa.Column("mediainfo", sa.JSON(), nullable=True),
        sa.Column("release_group", sa.String(length=100), nullable=True),
        sa.Column("original_name", sa.Text(), nullable=True),
        sa.Column("link_mode", sa.String(length=20), nullable=True),
        sa.Column("imported_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["destination_id"],
            ["destination.id"],
            name=op.f("fk_media_file_destination_id_destination"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["storage_node_id"],
            ["storage_node.id"],
            name=op.f("fk_media_file_storage_node_id_storage_node"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_file")),
        sa.UniqueConstraint(
            "storage_node_id", "relative_path", name="uq_media_file_storage_node_id_relative_path"
        ),
    )
    with op.batch_alter_table("media_file", schema=None) as batch_op:
        batch_op.create_index("ix_media_file_quick_hash", ["size", "quick_hash"], unique=False)

    op.create_table(
        "media_file_item",
        sa.Column("media_file_id", sa.String(length=26), nullable=False),
        sa.Column("wanted_item_id", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["media_file_id"],
            ["media_file.id"],
            name=op.f("fk_media_file_item_media_file_id_media_file"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["wanted_item_id"],
            ["wanted_item.id"],
            name=op.f("fk_media_file_item_wanted_item_id_wanted_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("media_file_id", "wanted_item_id", name=op.f("pk_media_file_item")),
    )
    with op.batch_alter_table("media_file_item", schema=None) as batch_op:
        batch_op.create_index("ix_media_file_item_wanted_item_id", ["wanted_item_id"], unique=False)

    with op.batch_alter_table("download", schema=None) as batch_op:
        batch_op.add_column(sa.Column("import_status", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("import_error", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("import_attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("import_retry_at", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("imported_at", UTCDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("download", schema=None) as batch_op:
        batch_op.drop_column("imported_at")
        batch_op.drop_column("import_retry_at")
        batch_op.drop_column("import_attempts")
        batch_op.drop_column("import_error")
        batch_op.drop_column("import_status")

    with op.batch_alter_table("media_file_item", schema=None) as batch_op:
        batch_op.drop_index("ix_media_file_item_wanted_item_id")

    op.drop_table("media_file_item")
    with op.batch_alter_table("media_file", schema=None) as batch_op:
        batch_op.drop_index("ix_media_file_quick_hash")

    op.drop_table("media_file")
    op.drop_table("destination")
    op.drop_table("storage_node")
    op.drop_table("naming_template")
