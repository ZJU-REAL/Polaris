"""Content-addressed PDF blobs and library asset grants.

Revision ID: c8d9e0f1a2b3
Revises: 8ff89f7fcdeb
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: str | None = "a7c8d9e0f1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "pdf_blobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False, server_default="application/pdf"),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("metadata_snapshot", _JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("sha256", name="uq_pdf_blobs_sha256"),
    )
    op.create_index("ix_pdf_blobs_sha256", "pdf_blobs", ["sha256"])

    op.create_table(
        "paper_assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("paper_id", sa.Uuid(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("blob_id", sa.Uuid(), sa.ForeignKey("pdf_blobs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_locator", sa.String(2048), nullable=True),
        sa.Column("identity_key", sa.String(512), nullable=True),
        sa.Column("identity_status", sa.String(16), nullable=False, server_default="unverified"),
        sa.Column("sharing_scope", sa.String(16), nullable=False, server_default="private"),
        sa.Column("state", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("is_preferred", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metadata_snapshot", _JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("paper_id", "blob_id", "source", name="uq_paper_assets_paper_blob_source"),
    )
    for name, columns in (
        ("ix_paper_assets_paper_id", ["paper_id"]),
        ("ix_paper_assets_blob_id", ["blob_id"]),
        ("ix_paper_assets_identity_key", ["identity_key"]),
        ("ix_paper_assets_paper_state", ["paper_id", "state"]),
    ):
        op.create_index(name, "paper_assets", columns)

    op.create_table(
        "asset_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("paper_assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("library_id", sa.Uuid(), sa.ForeignKey("direction_libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("can_read", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("can_process", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("granted_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("revoked_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("metadata_snapshot", _JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("asset_id", "library_id", name="uq_asset_grants_asset_library"),
    )
    op.create_index("ix_asset_grants_asset_id", "asset_grants", ["asset_id"])
    op.create_index("ix_asset_grants_library_id", "asset_grants", ["library_id"])
    op.create_index("ix_asset_grants_library_status", "asset_grants", ["library_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_asset_grants_library_status", table_name="asset_grants")
    op.drop_index("ix_asset_grants_library_id", table_name="asset_grants")
    op.drop_index("ix_asset_grants_asset_id", table_name="asset_grants")
    op.drop_table("asset_grants")
    for name in (
        "ix_paper_assets_paper_state",
        "ix_paper_assets_identity_key",
        "ix_paper_assets_blob_id",
        "ix_paper_assets_paper_id",
    ):
        op.drop_index(name, table_name="paper_assets")
    op.drop_table("paper_assets")
    op.drop_index("ix_pdf_blobs_sha256", table_name="pdf_blobs")
    op.drop_table("pdf_blobs")
