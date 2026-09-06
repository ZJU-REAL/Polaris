"""方法卡双轴向量表 method_vectors（#663：方法库 purpose–mechanism 双索引）

每篇论文的 method@1 抽取产物按 purpose / mechanism 两根轴各建一条向量，
主键 (paper_id, axis, space)。与三张既有向量侧表同口径：space 标识向量空间
（换嵌入模型走空间切换机制）、embedding 列不带维度（postgres 用 pgvector，
其他方言回退 JSON 只存不查）、不带 library_id（论文是共享内容池，库的边界
检索时经 library_papers 圈定）。

Revision ID: e867fcbae4ea
Revises: 58b0bc2d809d
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision: str = "e867fcbae4ea"
down_revision: str | None = "58b0bc2d809d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    is_postgres = op.get_bind().dialect.name == "postgresql"
    # postgres 用不带维度的 pgvector 列；其他方言回退 JSON（只存不查），
    # 与 5d8ebd5cb100 三张向量侧表同款
    vector_type = Vector() if is_postgres else sa.JSON()
    op.create_table(
        "method_vectors",
        sa.Column(
            "paper_id",
            sa.Uuid(),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("axis", sa.String(length=16), primary_key=True),
        sa.Column("space", sa.String(length=160), primary_key=True),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("embedding", vector_type, nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("text_version", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_method_vectors_space", "method_vectors", ["space"])


def downgrade() -> None:
    op.drop_index("ix_method_vectors_space", table_name="method_vectors")
    op.drop_table("method_vectors")
