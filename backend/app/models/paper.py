"""论文（文献综述对象）、概念（wiki 词条）、笔记 / 标签 / 个人阅读状态与其关联表。"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

EMBEDDING_DIM = 1024  # BGE-M3（lab LiteLLM /v1/embeddings）

# postgres 用 pgvector（语义检索），sqlite 等回退 JSON 存 list（仅存不查）
EmbeddingVariant = JSON().with_variant(Vector(EMBEDDING_DIM), "postgresql")

# 论文状态流转：candidate →(打分) scored | excluded →(下载全文) fetched
#              →(Librarian 编译) compiled；included/excluded 亦可人工覆盖
PAPER_STATUSES = ("candidate", "scored", "excluded", "fetched", "compiled", "included")

paper_concepts = Table(
    "paper_concepts",
    Base.metadata,
    Column("paper_id", ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("concept_id", ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True),
)


class Paper(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "papers"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source: Mapped[str | None] = mapped_column(String(32))  # arxiv | semantic_scholar | manual
    arxiv_id: Mapped[str | None] = mapped_column(String(64), index=True)
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    external_ids: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)  # {arxiv, s2, doi..}
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[list[Any] | None] = mapped_column(JSONVariant)  # [{"name": ...}]
    affiliations: Mapped[list[Any] | None] = mapped_column(JSONVariant)  # 发表机构 ["MIT", ...]
    abstract: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None]
    venue: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1024))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_path: Mapped[str | None] = mapped_column(String(1024))
    full_text_path: Mapped[str | None] = mapped_column(String(1024))
    relevance_score: Mapped[float | None]
    tldr: Mapped[str | None] = mapped_column(Text)
    wiki_content: Mapped[str | None] = mapped_column(Text)  # markdown，双链 [[概念名]]
    # 提取的论文图列表：[{index, page, width, height, caption: str|null, important: bool}]，
    # 图片文件落 <data_dir>/papers/<paper_id>/figures/fig_<index>.png（路径不出 API）
    figures: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVariant)
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    # 进垃圾桶的原因（status=excluded 时有值）：irrelevant 相关性不足自动淘汰 | manual 手动删除
    trash_reason: Mapped[str | None] = mapped_column(String(16))
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    concepts: Mapped[list["Concept"]] = relationship(
        secondary=paper_concepts, back_populates="papers"
    )

    @property
    def has_wiki(self) -> bool:
        return bool(self.wiki_content)

    @property
    def pdf_available(self) -> bool:
        return bool(self.pdf_path)


class PaperChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """论文全文分段（文献问答 / idea 生成的检索底座）。

    入库抽全文后确定性切分（services/chunks.py），embedding 由
    wiki.link_concepts 步骤批量补齐（provider 不支持时留空，检索降级关键词）。
    """

    __tablename__ = "paper_chunks"
    __table_args__ = (UniqueConstraint("paper_id", "seq", name="uq_paper_chunks_paper_seq"),)

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVariant)


READING_STATUSES = ("unread", "reading", "read")

paper_tag_links = Table(
    "paper_tag_links",
    Base.metadata,
    Column("paper_id", ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("paper_tags.id", ondelete="CASCADE"), primary_key=True),
)


class PaperNote(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """论文笔记：项目成员可读，作者（或平台 admin）可改删。"""

    __tablename__ = "paper_notes"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)


class PaperTag(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """项目级论文标签（同项目内名字唯一）；与论文多对多（paper_tag_links）。"""

    __tablename__ = "paper_tags"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_paper_tags_project_name"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class PaperUserMeta(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """论文的个人视角状态：星标 + 阅读状态（每人每篇至多一条）。"""

    __tablename__ = "paper_user_meta"
    __table_args__ = (UniqueConstraint("paper_id", "user_id", name="uq_paper_user_meta"),)

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    starred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reading_status: Mapped[str] = mapped_column(
        String(16), default="unread", nullable=False
    )  # unread | reading | read


class Concept(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "concepts"
    __table_args__ = (UniqueConstraint("project_id", "slug", name="uq_concepts_project_slug"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    definition: Mapped[str | None] = mapped_column(Text)  # LLM 一句话定义
    # method | architecture | methodology | problem | metric | dataset | other
    category: Mapped[str | None] = mapped_column(String(64))
    wiki_content: Mapped[str | None] = mapped_column(Text)  # markdown

    papers: Mapped[list[Paper]] = relationship(secondary=paper_concepts, back_populates="concepts")
