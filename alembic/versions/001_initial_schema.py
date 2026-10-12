"""001_initial_schema

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-10-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create links table
    op.create_table(
        "links",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("long_url", sa.String(length=2048), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "click_count",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_links_code"),
    )

    # Indexes on links table
    op.create_index(
        "idx_links_code_active",
        "links",
        ["code"],
        postgresql_where=sa.text("is_deleted = false"),
    )
    op.create_index(
        "idx_links_expires_at",
        "links",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )

    # 2. Create click_stats table
    op.create_table(
        "click_stats",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "count",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["code"],
            ["links.code"],
            name="fk_click_stats_code",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("code", "date", name="uq_click_stats_code_date"),
    )

    # Index on click_stats table
    op.create_index(
        "idx_click_stats_code_date",
        "click_stats",
        ["code", "date"],
    )


def downgrade() -> None:
    op.drop_index("idx_click_stats_code_date", table_name="click_stats")
    op.drop_table("click_stats")
    op.drop_index("idx_links_expires_at", table_name="links")
    op.drop_index("idx_links_code_active", table_name="links")
    op.drop_table("links")
