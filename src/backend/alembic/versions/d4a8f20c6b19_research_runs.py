"""research_runs: one row per research direction

A research run takes one direction typed by the user and drives it through scoping,
a literature survey and a hypothesis search. The row records which phase the run is
in and which task carries that phase; the tasks themselves are ordinary voyages.

Self-contained: one new table, nothing else is touched.

Revision ID: d4a8f20c6b19
Revises: c7e1a4b93d26
Create Date: 2026-11-18 13:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4a8f20c6b19"
down_revision: str | None = "c7e1a4b93d26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "research_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=True),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("brief", _JSON, nullable=True),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attention", _JSON, nullable=True),
        sa.Column("phases", _JSON, nullable=True),
        sa.Column("client_key", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["library_id"], ["direction_libraries.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_research_runs_project_id"),
        sa.UniqueConstraint("client_key", name="uq_research_runs_client_key"),
    )
    op.create_index("ix_research_runs_status", "research_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_research_runs_status", table_name="research_runs")
    op.drop_table("research_runs")
