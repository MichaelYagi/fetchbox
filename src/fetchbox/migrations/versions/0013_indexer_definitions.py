"""indexer_definition table; indexer.definition_id references it

Revision ID: 0013_indexer_definitions
Revises: 0012_seeding
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fetchbox.core.db import UTCDateTime

revision: str = "0013_indexer_definitions"
down_revision: str | None = "0012_seeding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "indexer_definition",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("yaml", sa.Text(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_indexer_definition")),
        sa.UniqueConstraint("slug", name=op.f("uq_indexer_definition_slug")),
    )
    with op.batch_alter_table("indexer", schema=None) as batch_op:
        batch_op.create_foreign_key(
            batch_op.f("fk_indexer_definition_id_indexer_definition"),
            "indexer_definition",
            ["definition_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("indexer", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_indexer_definition_id_indexer_definition"), type_="foreignkey"
        )
    op.drop_table("indexer_definition")
