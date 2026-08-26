"""Persist library-scoped literature discovery contracts.

Revision ID: a7c8d9e0f1b2
Revises: 8ff89f7fcdeb
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7c8d9e0f1b2"
down_revision: str | None = "8ff89f7fcdeb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "literature_search_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "library_id",
            sa.Uuid(),
            sa.ForeignKey("direction_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("candidate_budget", sa.Integer(), nullable=False),
        sa.Column("start_year", sa.Integer(), nullable=True),
        sa.Column("end_year", sa.Integer(), nullable=True),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("query_plan", _JSON, nullable=True),
        sa.Column("source_config", _JSON, nullable=True),
        sa.Column("model_version", sa.String(255), nullable=True),
        sa.Column("progress", _JSON, nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_literature_search_runs_library_id", "literature_search_runs", ["library_id"]
    )
    op.create_index(
        "ix_literature_search_runs_created_by", "literature_search_runs", ["created_by"]
    )
    op.create_index(
        "ix_literature_search_runs_library_status",
        "literature_search_runs",
        ["library_id", "status"],
    )

    op.create_table(
        "literature_search_hits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("literature_search_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "paper_id",
            sa.Uuid(),
            sa.ForeignKey("papers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="candidate"),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("dedup_key", sa.String(512), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("authors", _JSON, nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("venue", sa.String(512), nullable=True),
        sa.Column("doi", sa.String(255), nullable=True),
        sa.Column("pmid", sa.String(64), nullable=True),
        sa.Column("arxiv_id", sa.String(64), nullable=True),
        sa.Column("semantic_scholar_id", sa.String(128), nullable=True),
        sa.Column("url", sa.String(2048), nullable=True),
        sa.Column("pdf_url", sa.String(2048), nullable=True),
        sa.Column("oa_status", sa.String(32), nullable=True),
        sa.Column("citation_count", sa.Integer(), nullable=True),
        sa.Column("scores", _JSON, nullable=True),
        sa.Column("metadata_snapshot", _JSON, nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "dedup_key", name="uq_literature_search_hits_run_dedup"),
    )
    for name, column in (
        ("ix_literature_search_hits_run_id", "run_id"),
        ("ix_literature_search_hits_paper_id", "paper_id"),
        ("ix_literature_search_hits_doi", "doi"),
        ("ix_literature_search_hits_pmid", "pmid"),
        ("ix_literature_search_hits_arxiv_id", "arxiv_id"),
        ("ix_literature_search_hits_semantic_scholar_id", "semantic_scholar_id"),
    ):
        op.create_index(name, "literature_search_hits", [column])
    op.create_index(
        "ix_literature_search_hits_run_status",
        "literature_search_hits",
        ["run_id", "status"],
    )

    op.create_table(
        "literature_source_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("literature_search_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("cursor", sa.String(512), nullable=True),
        sa.Column("requested_count", sa.Integer(), nullable=True),
        sa.Column("fetched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accepted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("metadata_snapshot", _JSON, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "source", name="uq_literature_source_attempts_run_source"),
    )
    op.create_index(
        "ix_literature_source_attempts_run_id", "literature_source_attempts", ["run_id"]
    )
    op.create_index(
        "ix_literature_source_attempts_run_status",
        "literature_source_attempts",
        ["run_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_literature_source_attempts_run_status", table_name="literature_source_attempts")
    op.drop_index("ix_literature_source_attempts_run_id", table_name="literature_source_attempts")
    op.drop_table("literature_source_attempts")

    op.drop_index("ix_literature_search_hits_run_status", table_name="literature_search_hits")
    for name in (
        "ix_literature_search_hits_semantic_scholar_id",
        "ix_literature_search_hits_arxiv_id",
        "ix_literature_search_hits_pmid",
        "ix_literature_search_hits_doi",
        "ix_literature_search_hits_paper_id",
        "ix_literature_search_hits_run_id",
    ):
        op.drop_index(name, table_name="literature_search_hits")
    op.drop_table("literature_search_hits")

    op.drop_index(
        "ix_literature_search_runs_library_status", table_name="literature_search_runs"
    )
    op.drop_index("ix_literature_search_runs_created_by", table_name="literature_search_runs")
    op.drop_index("ix_literature_search_runs_library_id", table_name="literature_search_runs")
    op.drop_table("literature_search_runs")
