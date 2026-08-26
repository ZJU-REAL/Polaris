"""Versioned parsed PDF content and vectors.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: str | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "paper_content_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("paper_id", sa.Uuid(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_id", sa.Uuid(), sa.ForeignKey("paper_assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("parser", sa.String(32), nullable=False),
        sa.Column("parser_version", sa.String(128)),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("markdown_key", sa.String(1024)),
        sa.Column("text_key", sa.String(1024)),
        sa.Column("manifest_key", sa.String(1024)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("document_vector_state", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("chunk_vector_state", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("metadata_snapshot", _JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("paper_id", "version_no", name="uq_paper_content_versions_paper_no"),
    )
    op.create_index("ix_paper_content_versions_paper_id", "paper_content_versions", ["paper_id"])
    op.create_index("ix_paper_content_versions_asset_id", "paper_content_versions", ["asset_id"])
    op.create_index("ix_paper_content_versions_paper_status", "paper_content_versions", ["paper_id", "status"])

    op.create_table(
        "paper_content_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("content_version_id", sa.Uuid(), sa.ForeignKey("paper_content_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("page_start", sa.Integer()),
        sa.Column("page_end", sa.Integer()),
        sa.Column("rects", _JSON),
        sa.Column("section_path", _JSON),
        sa.Column("anchor_meta", _JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("content_version_id", "seq", name="uq_paper_content_chunks_version_seq"),
    )
    op.create_index("ix_paper_content_chunks_content_version_id", "paper_content_chunks", ["content_version_id"])
    op.create_index("ix_paper_content_chunks_version_seq", "paper_content_chunks", ["content_version_id", "seq"])

    for table, owner, fk, unique_name in (
        ("paper_content_version_vectors", "content_version_id", "paper_content_versions.id", "uq_content_version_vectors_space"),
        ("paper_content_chunk_vectors", "chunk_id", "paper_content_chunks.id", "uq_content_chunk_vectors_space"),
    ):
        op.create_table(
            table,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(owner, sa.Uuid(), sa.ForeignKey(fk, ondelete="CASCADE"), nullable=False),
            sa.Column("space", sa.String(160), nullable=False),
            sa.Column("dim", sa.Integer(), nullable=False),
            sa.Column("embedding", _JSON, nullable=False),
            sa.Column("model", sa.String(128), nullable=False),
            sa.Column("text_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(owner, "space", name=unique_name),
        )
        op.create_index(f"ix_{table}_{owner}", table, [owner])


def downgrade() -> None:
    for table, owner in (
        ("paper_content_chunk_vectors", "chunk_id"),
        ("paper_content_version_vectors", "content_version_id"),
    ):
        op.drop_index(f"ix_{table}_{owner}", table_name=table)
        op.drop_table(table)
    op.drop_index("ix_paper_content_chunks_version_seq", table_name="paper_content_chunks")
    op.drop_index("ix_paper_content_chunks_content_version_id", table_name="paper_content_chunks")
    op.drop_table("paper_content_chunks")
    for name in (
        "ix_paper_content_versions_paper_status",
        "ix_paper_content_versions_asset_id",
        "ix_paper_content_versions_paper_id",
    ):
        op.drop_index(name, table_name="paper_content_versions")
    op.drop_table("paper_content_versions")
