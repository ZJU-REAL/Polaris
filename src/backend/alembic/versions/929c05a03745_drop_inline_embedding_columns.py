"""删除主表上的向量列（向量已搬进侧表）

单独一个 revision，为的是必要时能只回滚这一步：上一步只加表不动旧数据，
两步之间的库既能被新代码读（走侧表），旧列也还在。

downgrade 重建列但**不搬回数据**：旧列写死 1024 维，而侧表里的向量可能是任意
维度，搬回去要么报错要么截断。回滚这一步的意义是让旧代码能启动，向量本身还在
侧表里，重新 upgrade 即恢复。

Revision ID: 929c05a03745
Revises: 5d8ebd5cb100
Create Date: 2026-07-27 20:44:14.587508

"""
from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '929c05a03745'
down_revision: str | None = '5d8ebd5cb100'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_DIM = 1024

# (表, 列)：向量本体 + 论文行上汇总的构建元信息
_DROPPED = (
    ("papers", "embedding"),
    ("papers", "embedding_model"),
    ("papers", "embedding_at"),
    ("papers", "chunk_embedding_model"),
    ("papers", "chunk_embedding_at"),
    ("paper_chunks", "embedding"),
    ("ideas", "embedding"),
)


def upgrade() -> None:
    for table, column in _DROPPED:
        op.drop_column(table, column)


def downgrade() -> None:
    is_postgres = op.get_bind().dialect.name == "postgresql"
    embedding_type = (
        sa.JSON().with_variant(Vector(LEGACY_DIM), "postgresql")
        if is_postgres
        else sa.JSON()
    )
    op.add_column("papers", sa.Column("embedding", embedding_type, nullable=True))
    op.add_column("papers", sa.Column("embedding_model", sa.String(length=128), nullable=True))
    op.add_column(
        "papers", sa.Column("embedding_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "papers", sa.Column("chunk_embedding_model", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "papers", sa.Column("chunk_embedding_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("paper_chunks", sa.Column("embedding", embedding_type, nullable=True))
    op.add_column("ideas", sa.Column("embedding", embedding_type, nullable=True))
