"""Persistent browser-extension download batches and leased items."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class DownloadApiKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "download_api_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    key_prefix: Mapped[str] = mapped_column(String(24), unique=True, nullable=False, index=True)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[Any]] = mapped_column(JSONVariant, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DownloadBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "download_batches"

    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DownloadBatchItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "download_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "library_id", "paper_id", name="uq_download_item_target"),
        Index("ix_download_items_owner_status", "created_by", "status"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("download_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expected_identity: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant, default=dict, nullable=False
    )
    article_url: Mapped[str | None] = mapped_column(Text)
    pdf_candidates: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(default=0, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    error: Mapped[str | None] = mapped_column(Text)
