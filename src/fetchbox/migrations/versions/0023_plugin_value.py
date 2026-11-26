"""plugin_value: the small key-value store each plugin keeps (ctx.store)

Revision ID: 0023_plugin_value
Revises: 0022_open_media_kinds
Create Date: 2026-11-26 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import fetchbox.core.db

revision: str = "0023_plugin_value"
down_revision: str | None = "0022_open_media_kinds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plugin_value",
        sa.Column("plugin", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_value")),
        sa.UniqueConstraint("plugin", "key", name="uq_plugin_value_plugin_key"),
    )
    with op.batch_alter_table("plugin_value", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_plugin_value_plugin"), ["plugin"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("plugin_value", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_plugin_value_plugin"))
    op.drop_table("plugin_value")
