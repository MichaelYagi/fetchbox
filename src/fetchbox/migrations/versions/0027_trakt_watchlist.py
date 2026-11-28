"""trakt watchlist: Trakt users whose watchlists are followed; a watchlist item is a Plex
account's or a Trakt user's

Revision ID: 0027_trakt_watchlist
Revises: 0026_plex_watchlist
Create Date: 2026-11-28 09:04:22.520841
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import fetchbox.core.db

revision: str = "0027_trakt_watchlist"
down_revision: str | None = "0026_plex_watchlist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trakt_user",
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_sync_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", fetchbox.core.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trakt_user")),
        sa.UniqueConstraint("username", name=op.f("uq_trakt_user_username")),
    )
    with op.batch_alter_table("watchlist_item", schema=None) as batch_op:
        batch_op.add_column(sa.Column("trakt_user_id", sa.String(length=26), nullable=True))
        batch_op.alter_column("account_id", existing_type=sa.VARCHAR(length=26), nullable=True)
        batch_op.create_unique_constraint(
            "uq_watchlist_item_trakt_user_id_plex_key", ["trakt_user_id", "plex_key"]
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_watchlist_item_trakt_user_id_trakt_user"),
            "trakt_user",
            ["trakt_user_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    # a Trakt user's items have no Plex account: they go first (the account is required again)
    op.execute("DELETE FROM watchlist_item WHERE account_id IS NULL")
    with op.batch_alter_table("watchlist_item", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_watchlist_item_trakt_user_id_trakt_user"), type_="foreignkey"
        )
        batch_op.drop_constraint("uq_watchlist_item_trakt_user_id_plex_key", type_="unique")
        batch_op.alter_column("account_id", existing_type=sa.VARCHAR(length=26), nullable=False)
        batch_op.drop_column("trakt_user_id")

    op.drop_table("trakt_user")
