"""schedules, idempotency records, api_key.version

Revision ID: 0005_conventions
Revises: 0004_activity
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fetchbox.core.db import UTCDateTime

revision: str = "0005_conventions"
down_revision: str | None = "0004_activity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schedule",
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("job_type", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("default_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("last_run_at", UTCDateTime(), nullable=True),
        sa.Column("next_run_at", UTCDateTime(), nullable=False),
        sa.Column("last_job_id", sa.String(26), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("name", name="pk_schedule"),
    )
    op.create_table(
        "idempotency_record",
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("actor_id", "key", name="pk_idempotency_record"),
    )
    op.create_index("ix_idempotency_record_created_at", "idempotency_record", ["created_at"])
    with op.batch_alter_table("api_key") as batch:
        # server_default fills existing rows; the ORM always writes it afterwards.
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    with op.batch_alter_table("api_key") as batch:
        batch.drop_column("version")
    op.drop_table("idempotency_record")
    op.drop_table("schedule")
