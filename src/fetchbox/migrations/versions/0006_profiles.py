"""quality profiles and custom formats

Revision ID: 0006_profiles
Revises: 0005_conventions
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fetchbox.core.db import UTCDateTime

revision: str = "0006_profiles"
down_revision: str | None = "0005_conventions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quality_profile",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("ladder", sa.JSON(), nullable=False),
        sa.Column("cutoff", sa.String(100), nullable=False),
        sa.Column("upgrade_allowed", sa.Boolean(), nullable=False),
        sa.Column("min_format_score", sa.Integer(), nullable=False),
        sa.Column("cutoff_format_score", sa.Integer(), nullable=True),
        sa.Column("upgrade_min_score_increment", sa.Integer(), nullable=False),
        sa.Column("format_scores", sa.JSON(), nullable=False),
        sa.Column("languages", sa.JSON(), nullable=False),
        sa.Column("sizes", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_quality_profile"),
        sa.UniqueConstraint("name", name="uq_quality_profile_name"),
    )
    op.create_table(
        "custom_format",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("default_score", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_custom_format"),
        sa.UniqueConstraint("name", name="uq_custom_format_name"),
    )


def downgrade() -> None:
    op.drop_table("custom_format")
    op.drop_table("quality_profile")
