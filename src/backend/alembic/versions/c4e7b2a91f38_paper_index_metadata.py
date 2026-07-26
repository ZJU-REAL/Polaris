"""分段来源标记 + 向量构建元信息（索引状态展示与手动重建）

文献对话只检索 paper_chunks，而分段过去只从 PDF 全文生成，没有全文的论文（每日推送
尤其严重——抓取时根本不下 PDF）对检索完全不存在。改成每篇论文至少有一个可检索分段：
有全文照旧按全文切，没有全文的用「标题 + 摘要」建一个块。

① paper_chunks.source：fulltext | abstract，区分两类块。存量分段都来自全文，
   server_default="fulltext" 直接回填；「这篇有没有全文索引」从此看它，不能只看
   有没有分段行（否则摘要块会让论文永远拿不到全文块）。
② papers 上四个向量元信息列：论文级向量与分块向量各自的构建时间与所用模型名。
   embedding 列本身只有向量，没有时间也没有模型名，前端要显示「构建时间 + 模型」
   就必须存。分块向量的元信息汇总记在 papers 上而不是每个 paper_chunks 行上——
   分段行有百万量级，逐行存两列时间/模型名纯属浪费。

存量数据的这四列留空（无从得知历史构建时间与模型），前端按「未构建」显示，重建一次
即补齐。

Revision ID: c4e7b2a91f38
Revises: b6c2f81d4a09
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e7b2a91f38"
down_revision: str | None = "b6c2f81d4a09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "paper_chunks",
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="fulltext",
        ),
    )
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


def downgrade() -> None:
    op.drop_column("papers", "chunk_embedding_at")
    op.drop_column("papers", "chunk_embedding_model")
    op.drop_column("papers", "embedding_at")
    op.drop_column("papers", "embedding_model")
    op.drop_column("paper_chunks", "source")
