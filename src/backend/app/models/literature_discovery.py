"""持久化的文献发现合同。

候选文献在用户晋升前独立于全局 ``Paper``，来源执行状态也独立记录。
真实来源适配器和前端工作台在后续 PR 中接入这些模型。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

SEARCH_RUN_STATUSES = ("queued", "running", "completed", "partial", "failed", "cancelled")
SEARCH_HIT_STATUSES = ("candidate", "promoted", "dismissed")
SOURCE_ATTEMPT_STATUSES = ("pending", "running", "completed", "partial", "failed", "skipped")


class LiteratureSearchRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次可复现的文献发现运行及其快照。"""

    __tablename__ = "literature_search_runs"
    __table_args__ = (Index("ix_literature_search_runs_library_status", "library_id", "status"),)

    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="queued", server_default="queued", nullable=False
    )
    requested_count: Mapped[int] = mapped_column(nullable=False)
    candidate_budget: Mapped[int] = mapped_column(nullable=False)
    start_year: Mapped[int | None]
    end_year: Mapped[int | None]
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    query_plan: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    source_config: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    model_version: Mapped[str | None] = mapped_column(String(255))
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    error_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LiteratureSearchHit(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """检索候选及其来源字段和评分证据。

    ``paper_id`` 在用户晋升前为空；候选记录不会提前污染全局论文池。
    """

    __tablename__ = "literature_search_hits"
    __table_args__ = (
        UniqueConstraint("run_id", "dedup_key", name="uq_literature_search_hits_run_dedup"),
        Index("ix_literature_search_hits_run_status", "run_id", "status"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("literature_search_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    paper_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="candidate", server_default="candidate", nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    abstract: Mapped[str | None] = mapped_column(Text)
    authors: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    year: Mapped[int | None]
    venue: Mapped[str | None] = mapped_column(String(512))
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    pmid: Mapped[str | None] = mapped_column(String(64), index=True)
    arxiv_id: Mapped[str | None] = mapped_column(String(64), index=True)
    semantic_scholar_id: Mapped[str | None] = mapped_column(String(128), index=True)
    url: Mapped[str | None] = mapped_column(String(2048))
    pdf_url: Mapped[str | None] = mapped_column(String(2048))
    oa_status: Mapped[str | None] = mapped_column(String(32))
    citation_count: Mapped[int | None]
    scores: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    metadata_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LiteratureSourceAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """某次运行对单个来源的执行状态、分页和失败信息。"""

    __tablename__ = "literature_source_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "source", name="uq_literature_source_attempts_run_source"),
        Index("ix_literature_source_attempts_run_status", "run_id", "status"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("literature_search_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", nullable=False
    )
    query: Mapped[str | None] = mapped_column(Text)
    cursor: Mapped[str | None] = mapped_column(String(512))
    requested_count: Mapped[int | None]
    fetched_count: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    accepted_count: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    retryable: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    metadata_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LiteratureOaCache(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persistent OA download state for a discovery hit.

    This cache is deliberately separate from ``PaperAsset``: discovery results
    may be cached before the user accepts them into a library.
    """

    __tablename__ = "literature_oa_caches"
    __table_args__ = (
        UniqueConstraint("hit_id", name="uq_literature_oa_caches_hit"),
        Index("ix_literature_oa_caches_status", "status"),
    )

    hit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("literature_search_hits.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(String(2048))
    final_url: Mapped[str | None] = mapped_column(String(2048))
    source: Mapped[str | None] = mapped_column(String(64))
    blob_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pdf_blobs.id", ondelete="SET NULL"), index=True
    )
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    byte_size: Mapped[int | None]
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LiteratureOaAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable audit row for each OA URL attempt."""

    __tablename__ = "literature_oa_attempts"
    __table_args__ = (Index("ix_literature_oa_attempts_cache", "cache_id"),)

    cache_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("literature_oa_caches.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[int | None]
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
