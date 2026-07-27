"""向量侧表：论文级 / 分段级 / 想法级各一张（见 app/core/embedding_space.py）。

向量从主表搬出来单独存，为的是**一个对象可以同时持有多个空间的向量**：换嵌入
模型时，新空间的向量可以在后台慢慢建，旧空间照常服务检索；建齐后改一下激活空间
设置就完成切换，不满意再切回去——旧向量一直都在。放在主表的一列里做不到这件事，
一列只装得下一套向量，换模型必然经历一段「大部分论文没向量」的窗口期。

``embedding`` 列**不带维度**（pgvector 允许 ``vector`` 不声明维度）。所以换一个
4096 维的模型不需要 ALTER TABLE，只需要新的 space 值——issue #191 要的就是这个。
代价是这样的列建不了 HNSW/IVFFlat 索引；现状五处向量检索本来就全是顺序扫描，
将来真要上 ANN，可以给激活空间单开一张定维表。

检索一律带 ``WHERE space = <激活空间>``，所以参与同一次比较的行维度必然一致。
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, DateTime, ForeignKey, Index, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import utcnow

# postgres 用不带维度的 pgvector 列；sqlite 等回退 JSON（只存不查，与主表旧列同款）
VectorVariant = JSON().with_variant(Vector(), "postgresql")


class _VectorColumns:
    """三张向量表共用的列。主键的另一半（owner 外键）由各表自己声明。"""

    # "<model>@<dim>"，如 "bge-m3@1024"。检索的唯一过滤依据。
    space: Mapped[str] = mapped_column(String(160), primary_key=True)
    # 冗余存一份维度：不用解析 space 字符串就能做体检/统计，也便于排查脏数据
    dim: Mapped[int] = mapped_column(nullable=False)
    embedding: Mapped[list[float]] = mapped_column(VectorVariant, nullable=False)
    # 建这条向量时路由表里的模型名。与 space 前半段同源，单独留一列是为了存量数据
    # 迁移时能保住原始模型名（存量的 space 可能是归并出来的）。
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    # 文本口径版本（喂给模型的那段文本怎么拼的）。口径变化不影响向量可比性，
    # 不进 space，只用于将来做选择性重建。
    text_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )
    built_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class PaperVector(_VectorColumns, Base):
    """论文级向量（标题+作者+摘要一条），语义检索与相似度排序用。"""

    __tablename__ = "paper_vectors"
    __table_args__ = (Index("ix_paper_vectors_space", "space"),)

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
    )


class PaperChunkVector(_VectorColumns, Base):
    """分段向量（每段一条），文献对话的细粒度检索用。行数比另两张高两三个数量级。"""

    __tablename__ = "paper_chunk_vectors"
    __table_args__ = (Index("ix_paper_chunk_vectors_space", "space"),)

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_chunks.id", ondelete="CASCADE"), primary_key=True
    )


class IdeaVector(_VectorColumns, Base):
    """想法向量，想法语义去重用。"""

    __tablename__ = "idea_vectors"
    __table_args__ = (Index("ix_idea_vectors_space", "space"),)

    idea_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ideas.id", ondelete="CASCADE"), primary_key=True
    )
