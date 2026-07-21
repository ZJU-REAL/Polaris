"""papers.embedding 1536 → 1024 维（BGE-M3）

嵌入切换到 lab LiteLLM 的 BGE-M3（1024 维）。旧向量是 fake 数据，直接清空后
改列类型；sqlite 分支 embedding 是 JSON variant，无需变更。

Revision ID: d6e7f8a9b0c1
Revises: c4d5e6f7a8b9
Create Date: 2026-07-14
"""

from alembic import op

revision = "d6e7f8a9b0c1"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # sqlite: JSON variant，列类型不变
    op.execute("UPDATE papers SET embedding = NULL")
    op.execute("ALTER TABLE papers ALTER COLUMN embedding TYPE vector(1024)")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("UPDATE papers SET embedding = NULL")
    op.execute("ALTER TABLE papers ALTER COLUMN embedding TYPE vector(1536)")
