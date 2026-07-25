"""llm_usage.created_at 建索引

实验室数据面板按天/周窗口聚合 token 用量（/lab/usage 与排行榜），
llm_usage 一直没有 created_at 索引，时间窗过滤会全表扫。

Revision ID: b7f3d21c8a05
Revises: c4a91e7b2f36
Create Date: 2026-07-25
"""

from alembic import op

revision = "b7f3d21c8a05"
down_revision = "c4a91e7b2f36"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_llm_usage_created_at", "llm_usage", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_created_at", table_name="llm_usage")
