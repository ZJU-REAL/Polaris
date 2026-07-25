"""相关研究书架 + 我的文献库 回收站（软删）

- topic_papers.trashed_at（timestamptz）/ trashed_by（Uuid，FK→users，SET NULL）：
  移出书架改为软删，可召回 / 彻底删除 / 清空。唯一键 uq_topic_papers_topic_paper
  覆盖软删行，同一篇再次入架走「复活」而不是插新行。
  索引 ix_topic_papers_topic_trashed(topic_id, trashed_at)：默认列表 / 回收站两条取行路径。
- user_library_entries.trashed_at（timestamptz）：取消收藏 / 删除条目进回收站，
  不再混进浏览记录。

存量行 trashed_at 全为 NULL = 都在架 / 都不在回收站，语义与迁移前一致。

Revision ID: a7f4c2e91b83
Revises: 49ddb68e7cf2
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7f4c2e91b83"
down_revision: str | None = "49ddb68e7cf2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("topic_papers") as batch:
        batch.add_column(sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("trashed_by", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_topic_papers_trashed_by", "users", ["trashed_by"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_topic_papers_topic_trashed", ["topic_id", "trashed_at"])
    op.add_column(
        "user_library_entries", sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("user_library_entries", "trashed_at")
    with op.batch_alter_table("topic_papers") as batch:
        batch.drop_index("ix_topic_papers_topic_trashed")
        batch.drop_constraint("fk_topic_papers_trashed_by", type_="foreignkey")
        batch.drop_column("trashed_by")
        batch.drop_column("trashed_at")
