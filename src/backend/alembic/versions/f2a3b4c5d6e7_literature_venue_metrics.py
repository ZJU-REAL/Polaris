"""Persist versioned literature venue metric snapshots and cache entries."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column("literature_search_hits", sa.Column("venue_metric_snapshot", _JSON))
    op.create_table(
        "literature_venue_metric_cache",
        sa.Column("provider", sa.String(32), primary_key=True),
        sa.Column("identity_key", sa.String(512), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("venue_name", sa.String(512)),
        sa.Column("issn_l", sa.String(16)),
        sa.Column("metrics", _JSON),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_literature_venue_metric_cache_expires_at",
        "literature_venue_metric_cache",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_literature_venue_metric_cache_expires_at",
        table_name="literature_venue_metric_cache",
    )
    op.drop_table("literature_venue_metric_cache")
    op.drop_column("literature_search_hits", "venue_metric_snapshot")
