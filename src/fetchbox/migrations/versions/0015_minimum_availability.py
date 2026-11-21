"""title.minimum_availability: when automatic search starts for a movie

Revision ID: 0015_minimum_availability
Revises: 0014_client_removal
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_minimum_availability"
down_revision: str | None = "0014_client_removal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("title", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "minimum_availability",
                sa.String(length=20),
                nullable=False,
                server_default="released",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("title", schema=None) as batch_op:
        batch_op.drop_column("minimum_availability")
