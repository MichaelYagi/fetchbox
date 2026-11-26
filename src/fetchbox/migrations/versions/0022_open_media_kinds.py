"""open media kinds: a title or library folder may be a plugin's kind, not only the core three

The kind columns kept only 'movie', 'series' and 'music'. They now take any kind name (2 to
10 lowercase characters); which kinds exist is the app's media kind list, which a plugin can
add to. Core rows are untouched.

Revision ID: 0022_open_media_kinds
Revises: 0021_media_file_sidecar
Create Date: 2026-11-26 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_open_media_kinds"
down_revision: str | None = "0021_media_file_sidecar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CORE = "('movie', 'series', 'music')"
TABLES = (("title", "kind"), ("destination", "media_kind"))


def _open(col: str) -> str:
    return f"length({col}) BETWEEN 2 AND 10 AND {col} = lower({col})"


def upgrade() -> None:
    for table, col in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(op.f(f"ck_{table}_{col}"), type_="check")
            batch_op.create_check_constraint(op.f(f"ck_{table}_{col}"), _open(col))


def downgrade() -> None:
    # Going back can't keep a plugin's titles or folders, and nothing is deleted to make it
    # fit: remove them in Fetchbox first (or turn the plugin's kind off), then downgrade.
    conn = op.get_bind()
    for table, col in TABLES:
        n = conn.execute(sa.text(f"SELECT count(*) FROM {table} WHERE {col} NOT IN {CORE}"))
        if count := n.scalar():
            raise RuntimeError(
                f"{count} {table} row(s) are a plugin's media kind; remove them first, "
                "nothing was changed"
            )
    for table, col in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(op.f(f"ck_{table}_{col}"), type_="check")
            batch_op.create_check_constraint(op.f(f"ck_{table}_{col}"), f"{col} IN {CORE}")
