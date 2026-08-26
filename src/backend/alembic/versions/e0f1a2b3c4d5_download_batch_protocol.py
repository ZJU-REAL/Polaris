"""Polaris browser extension download batches and API keys.

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e0f1a2b3c4d5"
down_revision: str | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "download_api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key_prefix", sa.String(24), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("scopes", _JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id"),
        sa.UniqueConstraint("key_prefix"),
    )
    op.create_index("ix_download_api_keys_user_id", "download_api_keys", ["user_id"])
    op.create_index("ix_download_api_keys_key_prefix", "download_api_keys", ["key_prefix"])

    op.create_table(
        "download_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_download_batches_created_by", "download_batches", ["created_by"])
    op.create_index("ix_download_batches_status", "download_batches", ["status"])

    op.create_table(
        "download_batch_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("batch_id", sa.Uuid(), sa.ForeignKey("download_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("library_id", sa.Uuid(), sa.ForeignKey("direction_libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("paper_id", sa.Uuid(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expected_identity", _JSON, nullable=False),
        sa.Column("article_url", sa.Text()),
        sa.Column("pdf_candidates", _JSON),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", _JSON),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("batch_id", "library_id", "paper_id", name="uq_download_item_target"),
    )
    op.create_index("ix_download_batch_items_batch_id", "download_batch_items", ["batch_id"])
    op.create_index("ix_download_batch_items_created_by", "download_batch_items", ["created_by"])
    op.create_index("ix_download_batch_items_library_id", "download_batch_items", ["library_id"])
    op.create_index("ix_download_batch_items_paper_id", "download_batch_items", ["paper_id"])
    op.create_index("ix_download_batch_items_status", "download_batch_items", ["status"])
    op.create_index(
        "ix_download_items_owner_status", "download_batch_items", ["created_by", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_download_items_owner_status", table_name="download_batch_items")
    for name in (
        "ix_download_batch_items_status",
        "ix_download_batch_items_paper_id",
        "ix_download_batch_items_library_id",
        "ix_download_batch_items_created_by",
        "ix_download_batch_items_batch_id",
    ):
        op.drop_index(name, table_name="download_batch_items")
    op.drop_table("download_batch_items")
    op.drop_index("ix_download_batches_status", table_name="download_batches")
    op.drop_index("ix_download_batches_created_by", table_name="download_batches")
    op.drop_table("download_batches")
    op.drop_index("ix_download_api_keys_key_prefix", table_name="download_api_keys")
    op.drop_index("ix_download_api_keys_user_id", table_name="download_api_keys")
    op.drop_table("download_api_keys")
