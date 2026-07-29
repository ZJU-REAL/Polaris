"""add library research digests and relevance reasons

Revision ID: e6a1c9d4f207
Revises: 929c05a03745
Create Date: 2026-07-29 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e6a1c9d4f207"
down_revision: str | None = "929c05a03745"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column("library_papers", sa.Column("relevance_reason", sa.Text(), nullable=True))
    op.create_table(
        "library_research_digests",
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("voyage_id", sa.Uuid(), nullable=True),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("counts", JSONVariant, nullable=False),
        sa.Column("source_diagnostics", JSONVariant, nullable=False),
        sa.Column("paper_insights", JSONVariant, nullable=False),
        sa.Column("excluded_papers", JSONVariant, nullable=False),
        sa.Column("cross_paper_signals", JSONVariant, nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("rolling_trends", JSONVariant, nullable=False),
        sa.Column("trend_content", sa.Text(), nullable=False),
        sa.Column("trend_model", sa.String(length=128), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["library_id"], ["direction_libraries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["voyage_id"], ["voyage_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "library_id",
            "report_date",
            "source",
            name="uq_library_research_digests_library_date_source",
        ),
        sa.UniqueConstraint("voyage_id", name="uq_library_research_digests_voyage"),
    )
    op.create_index(
        "ix_library_research_digests_library_date",
        "library_research_digests",
        ["library_id", "report_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_library_research_digests_library_date",
        table_name="library_research_digests",
    )
    op.drop_table("library_research_digests")
    op.drop_column("library_papers", "relevance_reason")
