"""Versioned parsed content for a paper PDF.

The PDF asset is immutable; every parser run gets its own content version so
reader anchors and embeddings never silently point at replaced text.
"""

import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

VectorVariant = JSON().with_variant(Vector(), "postgresql")


class PaperContentVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "paper_content_versions"
    __table_args__ = (
        UniqueConstraint("paper_id", "version_no", name="uq_paper_content_versions_paper_no"),
        Index("ix_paper_content_versions_paper_status", "paper_id", "status"),
    )

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parser: Mapped[str] = mapped_column(String(32), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    markdown_key: Mapped[str | None] = mapped_column(String(1024))
    text_key: Mapped[str | None] = mapped_column(String(1024))
    manifest_key: Mapped[str | None] = mapped_column(String(1024))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    document_vector_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending"
    )
    chunk_vector_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending"
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)


class PaperContentChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "paper_content_chunks"
    __table_args__ = (
        UniqueConstraint("content_version_id", "seq", name="uq_paper_content_chunks_version_seq"),
        Index("ix_paper_content_chunks_version_seq", "content_version_id", "seq"),
    )

    content_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_content_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    rects: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    section_path: Mapped[list[str] | None] = mapped_column(JSONVariant)
    anchor_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)


class PaperContentVersionVector(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "paper_content_version_vectors"
    __table_args__ = (
        UniqueConstraint(
            "content_version_id", "space", name="uq_content_version_vectors_space"
        ),
    )

    content_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_content_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    space: Mapped[str] = mapped_column(String(160), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(VectorVariant, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    text_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class PaperContentChunkVector(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "paper_content_chunk_vectors"
    __table_args__ = (UniqueConstraint("chunk_id", "space", name="uq_content_chunk_vectors_space"),)

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_content_chunks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    space: Mapped[str] = mapped_column(String(160), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(VectorVariant, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    text_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
