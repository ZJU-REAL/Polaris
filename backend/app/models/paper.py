"""论文（文献综述对象）、概念（wiki 词条）与其关联表。"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Column, DateTime, ForeignKey, String, Table, Text, UniqueConstraint
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
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVariant)
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
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
