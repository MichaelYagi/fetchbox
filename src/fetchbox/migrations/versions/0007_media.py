"""runtime config, titles, seasons, episodes, wanted items

Revision ID: 0007_media
Revises: 0006_profiles
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fetchbox.core.db import UTCDateTime

revision: str = "0007_media"
down_revision: str | None = "0006_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_config",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_config")),
    )
    op.create_table(
        "title",
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("tvdb_id", sa.Integer(), nullable=True),
        sa.Column("imdb_id", sa.String(length=20), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("sort_name", sa.String(length=500), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("profile_id", sa.String(length=26), nullable=False),
        sa.Column("destination_id", sa.String(length=26), nullable=True),
        sa.Column("monitor_mode", sa.String(length=20), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("added_at", UTCDateTime(), nullable=False),
        sa.Column("refreshed_at", UTCDateTime(), nullable=True),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("kind IN ('movie', 'series')", name=op.f("ck_title_kind")),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["quality_profile.id"], name=op.f("fk_title_profile_id_quality_profile")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_title")),
        sa.UniqueConstraint("kind", "tmdb_id", name="uq_title_kind_tmdb_id"),
    )
    with op.batch_alter_table("title", schema=None) as batch_op:
        batch_op.create_index("ix_title_sort_name", ["sort_name"], unique=False)

    op.create_table(
        "season",
        sa.Column("title_id", sa.String(length=26), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("monitored", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["title_id"], ["title.id"], name=op.f("fk_season_title_id_title"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_season")),
        sa.UniqueConstraint("title_id", "number", name="uq_season_title_id_number"),
    )
    op.create_table(
        "episode",
        sa.Column("season_id", sa.String(length=26), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("absolute_number", sa.Integer(), nullable=True),
        sa.Column("air_date", sa.Date(), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=True),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["season_id"],
            ["season.id"],
            name=op.f("fk_episode_season_id_season"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episode")),
        sa.UniqueConstraint("season_id", "number", name="uq_episode_season_id_number"),
    )
    op.create_table(
        "wanted_item",
        sa.Column("title_id", sa.String(length=26), nullable=False),
        sa.Column("episode_id", sa.String(length=26), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("monitored", sa.Boolean(), nullable=False),
        sa.Column("lock", sa.String(length=10), nullable=True),
        sa.Column("cutoff_met", sa.Boolean(), nullable=False),
        sa.Column("last_search_at", UTCDateTime(), nullable=True),
        sa.Column("next_search_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "lock IS NULL OR lock IN ('acquired', 'manual')", name=op.f("ck_wanted_item_lock")
        ),
        sa.CheckConstraint(
            "state IN ('missing', 'searching', 'grabbed', 'downloading', 'importing', 'acquired')",
            name=op.f("ck_wanted_item_state"),
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episode.id"],
            name=op.f("fk_wanted_item_episode_id_episode"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["title_id"],
            ["title.id"],
            name=op.f("fk_wanted_item_title_id_title"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wanted_item")),
        sa.UniqueConstraint("title_id", "episode_id", name="uq_wanted_item_title_id_episode_id"),
    )
    with op.batch_alter_table("wanted_item", schema=None) as batch_op:
        batch_op.create_index("ix_wanted_item_title_id", ["title_id"], unique=False)
        batch_op.create_index(
            "ix_wanted_search", ["monitored", "lock", "state", "next_search_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("wanted_item", schema=None) as batch_op:
        batch_op.drop_index("ix_wanted_search")
        batch_op.drop_index("ix_wanted_item_title_id")

    op.drop_table("wanted_item")
    op.drop_table("episode")
    op.drop_table("season")
    with op.batch_alter_table("title", schema=None) as batch_op:
        batch_op.drop_index("ix_title_sort_name")

    op.drop_table("title")
    op.drop_table("app_config")
