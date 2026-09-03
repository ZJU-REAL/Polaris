"""移除技能市场评分表 skill_ratings（部署内互评，个人化定位下撤除）

同批移除：市场审核流（approve/reject/pending 队列）；发布改为直接上架。
skill_listings 的 status 列保留（approved/delisted 仍在用）。

Revision ID: d4b8e26f1a93
Revises: c8f2a61d9e37
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4b8e26f1a93"
down_revision: str | None = "c8f2a61d9e37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(op.f("ix_skill_ratings_listing_id"), table_name="skill_ratings")
    op.drop_table("skill_ratings")


def downgrade() -> None:
    # 复原 f5a6b7c8d9e0 的形状（数据不可恢复）。
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
