"""plugin: the plugins Fetchbox has found, whether each may run, its settings and error

Revision ID: 0024_plugin
Revises: 0023_plugin_value
Create Date: 2026-11-26 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import fetchbox.core.db

revision: str = "0024_plugin"
down_revision: str | None = "0023_plugin_value"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plugin",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("found_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin")),
    )


def downgrade() -> None:
    op.drop_table("plugin")
