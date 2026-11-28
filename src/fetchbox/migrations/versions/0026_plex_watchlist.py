"""plex watchlist: Plex accounts signed in to follow their watchlists, and what each
watchlisted title became (added, waiting for approval, already there, dismissed)

Revision ID: 0026_plex_watchlist
Revises: 0025_plugin_item
Create Date: 2026-11-28 07:45:49.303265
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import fetchbox.core.db

revision: str = "0026_plex_watchlist"
down_revision: str | None = "0025_plugin_item"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plex_account",
        sa.Column("plex_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=200), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_sync_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plex_account")),
        sa.UniqueConstraint("plex_id", name=op.f("uq_plex_account_plex_id")),
    )
    op.create_table(
        "watchlist_item",
        sa.Column("account_id", sa.String(length=26), nullable=False),
        sa.Column("plex_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("title_id", sa.String(length=26), nullable=True),
        sa.Column("seen_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["plex_account.id"],
            name=op.f("fk_watchlist_item_account_id_plex_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlist_item")),
        sa.UniqueConstraint("account_id", "plex_key", name="uq_watchlist_item_account_id_plex_key"),
    )
    with op.batch_alter_table("watchlist_item", schema=None) as batch_op:
        batch_op.create_index("ix_watchlist_item_state", ["state"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("watchlist_item", schema=None) as batch_op:
        batch_op.drop_index("ix_watchlist_item_state")

    op.drop_table("watchlist_item")
    op.drop_table("plex_account")
