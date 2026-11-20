"""job table

Revision ID: 0002_job
Revises: 0001_baseline
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fetchbox.core.db import UTCDateTime

revision: str = "0002_job"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE = sa.text("status IN ('pending','running')")


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("type", sa.String(100), nullable=False),
        sa.Column("queue", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", UTCDateTime(), nullable=False),
        sa.Column("locked_by", sa.String(64), nullable=True),
        sa.Column("locked_until", UTCDateTime(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column(
            "parent_id",
            sa.String(26),
            sa.ForeignKey("job.id", name="fk_job_parent_id_job", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=True),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_job"),
    )
    op.create_index("ix_job_ready", "job", ["queue", "status", "run_after"])
    op.create_index("ix_job_parent", "job", ["parent_id"])
    op.create_index(
        "uq_job_dedupe_active",
        "job",
        ["dedupe_key"],
        unique=True,
        sqlite_where=ACTIVE,
        postgresql_where=ACTIVE,
    )


def downgrade() -> None:
    op.drop_table("job")
