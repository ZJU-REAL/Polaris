"""论文（文献综述对象）、概念（wiki 词条）与其关联表。"""

import uuid
from typing import Any

from sqlalchemy import Column, ForeignKey, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

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
    arxiv_id: Mapped[str | None] = mapped_column(String(64), index=True)
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    abstract: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None]
    venue: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1024))
    pdf_path: Mapped[str | None] = mapped_column(String(1024))
    relevance_score: Mapped[float | None]
    wiki_content: Mapped[str | None] = mapped_column(Text)  # markdown
    # candidate | scored | compiled | excluded
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)

    concepts: Mapped[list["Concept"]] = relationship(
        secondary=paper_concepts, back_populates="papers"
    )


class Concept(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "concepts"

    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    definition: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(64))
    wiki_content: Mapped[str | None] = mapped_column(Text)  # markdown

    papers: Mapped[list[Paper]] = relationship(secondary=paper_concepts, back_populates="concepts")
