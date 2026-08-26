"""Add immutable sentence and paragraph evidence anchors.

Revision ID: e5f6a7b8c9d0
Revises: d9e0f1a2b3c4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "paper_evidence_anchors",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "paper_id",
            sa.Uuid(),
            sa.ForeignKey("papers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id",
            sa.Uuid(),
            sa.ForeignKey("paper_chunks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("content_revision", sa.String(64), nullable=False),
        sa.Column("anchor_key", sa.String(255), nullable=False),
        sa.Column("anchor_type", sa.String(16), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=True),
        sa.Column("paragraph_index", sa.Integer(), nullable=True),
        sa.Column("sentence_index", sa.Integer(), nullable=True),
        sa.Column("quoted_text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("locator", _JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "paper_id",
            "content_revision",
            "anchor_key",
            name="uq_paper_evidence_anchors_revision_key",
        ),
    )
    op.create_index(
        "ix_paper_evidence_anchors_paper_id", "paper_evidence_anchors", ["paper_id"]
    )
    op.create_index(
        "ix_paper_evidence_anchors_paper_revision",
        "paper_evidence_anchors",
        ["paper_id", "content_revision"],
    )
    op.create_index(
        "ix_paper_evidence_anchors_chunk_id", "paper_evidence_anchors", ["chunk_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_paper_evidence_anchors_chunk_id", table_name="paper_evidence_anchors")
    op.drop_index(
        "ix_paper_evidence_anchors_paper_revision", table_name="paper_evidence_anchors"
    )
    op.drop_index("ix_paper_evidence_anchors_paper_id", table_name="paper_evidence_anchors")
    op.drop_table("paper_evidence_anchors")
