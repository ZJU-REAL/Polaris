"""Content-addressed PDF blobs, paper assets, and library-scoped grants."""

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class PdfBlob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable PDF byte sequence, addressed by SHA-256."""

    __tablename__ = "pdf_blobs"
    __table_args__ = (UniqueConstraint("sha256", name="uq_pdf_blobs_sha256"),)

    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(128), nullable=False, default="application/pdf"
    )
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    metadata_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)


class PaperAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A paper's provenance and sharing record for one PDF blob."""

    __tablename__ = "paper_assets"
    __table_args__ = (
        UniqueConstraint(
            "paper_id", "blob_id", "source", name="uq_paper_assets_paper_blob_source"
        ),
        Index("ix_paper_assets_paper_state", "paper_id", "state"),
    )

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    blob_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pdf_blobs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_locator: Mapped[str | None] = mapped_column(String(2048))
    identity_key: Mapped[str | None] = mapped_column(String(512), index=True)
    identity_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unverified")
    sharing_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="private")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    is_preferred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)


class AssetGrant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Explicit permission for a library to read/process a paper asset."""

    __tablename__ = "asset_grants"
    __table_args__ = (
        UniqueConstraint("asset_id", "library_id", name="uq_asset_grants_asset_library"),
        Index("ix_asset_grants_library_status", "library_id", "status"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    can_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_process: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    metadata_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
