"""download.client_id nullable, SET NULL when its client is removed

Revision ID: 0014_client_removal
Revises: 0013_indexer_definitions
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_client_removal"
down_revision: str | None = "0013_indexer_definitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK = "fk_download_client_id_download_client"


def upgrade() -> None:
    with op.batch_alter_table("download", schema=None) as batch_op:
        batch_op.alter_column("client_id", existing_type=sa.String(length=26), nullable=True)
        batch_op.drop_constraint(FK, type_="foreignkey")
        batch_op.create_foreign_key(
            FK, "download_client", ["client_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("download", schema=None) as batch_op:
        batch_op.drop_constraint(FK, type_="foreignkey")
        batch_op.create_foreign_key(FK, "download_client", ["client_id"], ["id"])
        batch_op.alter_column("client_id", existing_type=sa.String(length=26), nullable=False)
