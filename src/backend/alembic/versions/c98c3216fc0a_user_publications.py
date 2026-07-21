"""user author profiles + user publications (my publications, issue #109)

Revision ID: c98c3216fc0a
Revises: 57e55702bcca
Create Date: 2026-07-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c98c3216fc0a"
down_revision: str | None = "57e55702bcca"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_author_profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name_variants", sa.JSON(), nullable=False),
        sa.Column("affiliations", sa.JSON(), nullable=False),
        sa.Column("openalex_author_id", sa.String(length=64), nullable=True),
        sa.Column("s2_author_id", sa.String(length=64), nullable=True),
        sa.Column("orcid", sa.String(length=32), nullable=True),
        sa.Column("auto_sync", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_author_profiles_user_id", "user_author_profiles", ["user_id"], unique=True
    )
    op.create_table(
        "user_publications",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("dedup_key", sa.String(length=512), nullable=False),
        sa.Column("openalex_id", sa.String(length=64), nullable=True),
        sa.Column("arxiv_id", sa.String(length=64), nullable=True),
        sa.Column("doi", sa.String(length=255), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("authors", sa.JSON(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("venue", sa.String(length=255), nullable=True),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column("cited_by_count", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "dedup_key", name="uq_user_publications_dedup"),
    )
    op.create_index(
        "ix_user_publications_user_id", "user_publications", ["user_id"], unique=False
    )
    op.create_index("ix_user_publications_status", "user_publications", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_user_publications_status", table_name="user_publications")
    op.drop_index("ix_user_publications_user_id", table_name="user_publications")
    op.drop_table("user_publications")
    op.drop_index("ix_user_author_profiles_user_id", table_name="user_author_profiles")
    op.drop_table("user_author_profiles")
