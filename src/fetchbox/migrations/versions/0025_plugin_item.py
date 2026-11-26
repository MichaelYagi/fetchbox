"""plugin_item: the items of a plugin media kind's titles (issues of a comic series), and
the wanted item that is each one

Revision ID: 0025_plugin_item
Revises: 0024_plugin
Create Date: 2026-11-26 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_plugin_item"
down_revision: str | None = "0024_plugin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plugin_item",
        sa.Column("title_id", sa.String(length=26), nullable=False),
        sa.Column("ref", sa.String(length=200), nullable=False),
        sa.Column("number", sa.Float(), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=True),
        sa.Column("released", sa.Date(), nullable=True),
        sa.Column("group_ref", sa.String(length=200), nullable=True),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["title_id"],
            ["title.id"],
            name=op.f("fk_plugin_item_title_id_title"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_item")),
        sa.UniqueConstraint("title_id", "ref", name="uq_plugin_item_title_id_ref"),
    )
    with op.batch_alter_table("wanted_item", schema=None) as batch_op:
        batch_op.add_column(sa.Column("plugin_item_id", sa.String(length=26), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_wanted_item_plugin_item_id_plugin_item"), "plugin_item",
            ["plugin_item_id"], ["id"], ondelete="CASCADE",
        )  # fmt: skip
        batch_op.create_unique_constraint(
            "uq_wanted_item_title_id_plugin_item_id", ["title_id", "plugin_item_id"]
        )


def downgrade() -> None:
    # Nothing is deleted to go back: a plugin's titles are removed in Fetchbox first.
    n = op.get_bind().execute(sa.text("SELECT count(*) FROM plugin_item")).scalar()
    if n:
        raise RuntimeError(
            f"{n} plugin item(s) exist; remove the plugin's titles first, nothing was changed"
        )
    with op.batch_alter_table("wanted_item", schema=None) as batch_op:
        batch_op.drop_constraint("uq_wanted_item_title_id_plugin_item_id", type_="unique")
        batch_op.drop_constraint(
            batch_op.f("fk_wanted_item_plugin_item_id_plugin_item"), type_="foreignkey"
        )
        batch_op.drop_column("plugin_item_id")
    op.drop_table("plugin_item")
