"""skill market S4 (docs/skill-system.md §4.3): skill_listings / skill_ratings

- skill_listings：市场条目（指向具体 skill_version，管理员审核 pending→approved）
- skill_ratings：评分（uq(listing, user)，可更新）

Revision ID: f5a6b7c8d9e0
Revises: e2f3a4b5c6d7
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f5a6b7c8d9e0"
down_revision: str | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_listings",
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("install_count", sa.Integer(), nullable=False),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_skill_listings_skill_id"), "skill_listings", ["skill_id"], unique=False
    )
    op.create_index(op.f("ix_skill_listings_status"), "skill_listings", ["status"], unique=False)

    op.create_table(
        "skill_ratings",
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["skill_listings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("listing_id", "user_id", name="uq_skill_ratings_user"),
    )
    op.create_index(
        op.f("ix_skill_ratings_listing_id"), "skill_ratings", ["listing_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_skill_ratings_listing_id"), table_name="skill_ratings")
    op.drop_table("skill_ratings")
    op.drop_index(op.f("ix_skill_listings_status"), table_name="skill_listings")
    op.drop_index(op.f("ix_skill_listings_skill_id"), table_name="skill_listings")
    op.drop_table("skill_listings")
