"""grabs, blocklist, history

Revision ID: 0009_grabs
Revises: 0008_indexers
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fetchbox.core.db import UTCDateTime

revision: str = "0009_grabs"
down_revision: str | None = "0008_indexers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "history_event",
        sa.Column("wanted_item_id", sa.String(length=26), nullable=True),
        sa.Column("title_id", sa.String(length=26), nullable=True),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_history_event")),
    )
    with op.batch_alter_table("history_event", schema=None) as batch_op:
        batch_op.create_index("ix_history_event_title_id", ["title_id", "created_at"], unique=False)
        batch_op.create_index("ix_history_event_wanted_item_id", ["wanted_item_id"], unique=False)

    op.create_table(
        "grab",
        sa.Column("release", sa.JSON(), nullable=False),
        sa.Column("indexer_id", sa.String(length=26), nullable=True),
        sa.Column("info_hash", sa.String(length=40), nullable=True),
        sa.Column("release_title", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(length=10), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.CheckConstraint(
            "reason IN ('manual', 'rss', 'search', 'upgrade')", name=op.f("ck_grab_reason")
        ),
        sa.ForeignKeyConstraint(
            ["indexer_id"],
            ["indexer.id"],
            name=op.f("fk_grab_indexer_id_indexer"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grab")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_grab_idempotency_key")),
    )
    with op.batch_alter_table("grab", schema=None) as batch_op:
        batch_op.create_index("ix_grab_info_hash", ["info_hash"], unique=False)

    op.create_table(
        "blocklist",
        sa.Column("title_id", sa.String(length=26), nullable=True),
        sa.Column("info_hash", sa.String(length=40), nullable=True),
        sa.Column("guid", sa.Text(), nullable=True),
        sa.Column("release_title", sa.Text(), nullable=True),
        sa.Column("indexer_id", sa.String(length=26), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.CheckConstraint(
            "info_hash IS NOT NULL OR guid IS NOT NULL OR release_title IS NOT NULL",
            name=op.f("ck_blocklist_identifies"),
        ),
        sa.ForeignKeyConstraint(
            ["indexer_id"],
            ["indexer.id"],
            name=op.f("fk_blocklist_indexer_id_indexer"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["title_id"], ["title.id"], name=op.f("fk_blocklist_title_id_title"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blocklist")),
    )
    with op.batch_alter_table("blocklist", schema=None) as batch_op:
        batch_op.create_index("ix_blocklist_info_hash", ["info_hash"], unique=False)
        batch_op.create_index("ix_blocklist_title_id", ["title_id"], unique=False)

    op.create_table(
        "grab_item",
        sa.Column("grab_id", sa.String(length=26), nullable=False),
        sa.Column("wanted_item_id", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["grab_id"], ["grab.id"], name=op.f("fk_grab_item_grab_id_grab"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["wanted_item_id"],
            ["wanted_item.id"],
            name=op.f("fk_grab_item_wanted_item_id_wanted_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("grab_id", "wanted_item_id", name=op.f("pk_grab_item")),
    )
    with op.batch_alter_table("grab_item", schema=None) as batch_op:
        batch_op.create_index("ix_grab_item_wanted_item_id", ["wanted_item_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("grab_item", schema=None) as batch_op:
        batch_op.drop_index("ix_grab_item_wanted_item_id")

    op.drop_table("grab_item")
    with op.batch_alter_table("blocklist", schema=None) as batch_op:
        batch_op.drop_index("ix_blocklist_title_id")
        batch_op.drop_index("ix_blocklist_info_hash")

    op.drop_table("blocklist")
    with op.batch_alter_table("grab", schema=None) as batch_op:
        batch_op.drop_index("ix_grab_info_hash")

    op.drop_table("grab")
    with op.batch_alter_table("history_event", schema=None) as batch_op:
        batch_op.drop_index("ix_history_event_wanted_item_id")
        batch_op.drop_index("ix_history_event_title_id")

    op.drop_table("history_event")
