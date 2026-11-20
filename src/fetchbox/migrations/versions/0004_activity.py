"""activity log

Revision ID: 0004_activity
Revises: 0003_auth
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fetchbox.core.db import UTCDateTime

revision: str = "0004_activity"
down_revision: str | None = "0003_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "activity",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(100), nullable=True),
        sa.Column("subject_type", sa.String(32), nullable=True),
        sa.Column("subject_id", sa.String(64), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_activity"),
    )
    op.create_index("ix_activity_category", "activity", ["category"])
    op.create_index("ix_activity_subject", "activity", ["subject_type", "subject_id"])
    op.create_index("ix_activity_created_at", "activity", ["created_at"])


def downgrade() -> None:
    op.drop_table("activity")
