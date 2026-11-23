"""Title.folder: the title's own folder inside its library folder

Revision ID: 0020_title_folder
Revises: 0019_music
Create Date: 2026-11-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_title_folder"
down_revision: str | None = "0019_music"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("title") as batch:
        batch.add_column(sa.Column("folder", sa.String(length=1000), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("title") as batch:
        batch.drop_column("folder")
