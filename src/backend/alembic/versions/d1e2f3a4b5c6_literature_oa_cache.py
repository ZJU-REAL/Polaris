"""Persistent OA cache and promotion audit for literature discovery."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "e0f1a2b3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "literature_oa_caches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "hit_id",
            sa.Uuid(),
            sa.ForeignKey("literature_search_hits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("source_url", sa.String(2048)),
        sa.Column("final_url", sa.String(2048)),
        sa.Column("source", sa.String(64)),
        sa.Column("blob_id", sa.Uuid(), sa.ForeignKey("pdf_blobs.id", ondelete="SET NULL")),
        sa.Column("sha256", sa.String(64)),
        sa.Column("byte_size", sa.Integer()),
        sa.Column("verification", _JSON),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("downloaded_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("hit_id", name="uq_literature_oa_caches_hit"),
    )
    op.create_index("ix_literature_oa_caches_status", "literature_oa_caches", ["status"])
    op.create_index("ix_literature_oa_caches_blob_id", "literature_oa_caches", ["blob_id"])
    op.create_index("ix_literature_oa_caches_sha256", "literature_oa_caches", ["sha256"])

    op.create_table(
        "literature_oa_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "cache_id",
            sa.Uuid(),
            sa.ForeignKey("literature_oa_caches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("verification", _JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_literature_oa_attempts_cache", "literature_oa_attempts", ["cache_id"])


def downgrade() -> None:
    op.drop_index("ix_literature_oa_attempts_cache", table_name="literature_oa_attempts")
    op.drop_table("literature_oa_attempts")
    op.drop_index("ix_literature_oa_caches_sha256", table_name="literature_oa_caches")
    op.drop_index("ix_literature_oa_caches_blob_id", table_name="literature_oa_caches")
    op.drop_index("ix_literature_oa_caches_status", table_name="literature_oa_caches")
    op.drop_table("literature_oa_caches")
