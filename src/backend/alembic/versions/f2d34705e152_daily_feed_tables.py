"""每日新论文池（Daily Paper）

新建 daily_feed_entries（池橱窗行：paper_id 唯一引用内容池、feed_date 滚动 7 天、
分类与公告类型、共享单篇解读）与 daily_feed_likes（全实验室共享点赞，
entry 过期删除时级联跟删）。

Revision ID: f2d34705e152
Revises: 47b973fb7e13
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

revision: str = "f2d34705e152"
down_revision: str | None = "47b973fb7e13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_feed_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("feed_date", sa.Date(), nullable=False),
        sa.Column("primary_category", sa.String(length=32), nullable=False),
        sa.Column("categories", _JSON, nullable=False),
        sa.Column("announce_type", sa.String(length=16), nullable=False),
        sa.Column("wiki_content", sa.Text(), nullable=True),
        sa.Column("wiki_model", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("paper_id", name="uq_daily_feed_entries_paper_id"),
    )
    op.create_index("ix_daily_feed_entries_feed_date", "daily_feed_entries", ["feed_date"])

    op.create_table(
        "daily_feed_likes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entry_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["entry_id"], ["daily_feed_entries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("entry_id", "user_id", name="uq_daily_feed_likes"),
    )
    op.create_index("ix_daily_feed_likes_entry_id", "daily_feed_likes", ["entry_id"])
    op.create_index("ix_daily_feed_likes_user_id", "daily_feed_likes", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_daily_feed_likes_user_id", table_name="daily_feed_likes")
    op.drop_index("ix_daily_feed_likes_entry_id", table_name="daily_feed_likes")
    op.drop_table("daily_feed_likes")
    op.drop_index("ix_daily_feed_entries_feed_date", table_name="daily_feed_entries")
    op.drop_table("daily_feed_entries")
