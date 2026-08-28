"""Persist versioned translations for discovery metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f4a5b6c7d8e9"
down_revision: str = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "literature_hit_translations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "hit_id",
            sa.Uuid(),
            sa.ForeignKey("literature_search_hits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_language", sa.String(32), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("translated_fields", _JSON),
        sa.Column("error_code", sa.String(64)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "hit_id",
            "target_language",
            "source_hash",
            "model_version",
            name="uq_literature_hit_translation_cache",
        ),
    )
    op.create_index(
        "ix_literature_hit_translations_hit_id",
        "literature_hit_translations",
        ["hit_id"],
    )
    op.create_index(
        "ix_literature_hit_translations_hit_status",
        "literature_hit_translations",
        ["hit_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_literature_hit_translations_hit_status",
        table_name="literature_hit_translations",
    )
    op.drop_index(
        "ix_literature_hit_translations_hit_id",
        table_name="literature_hit_translations",
    )
    op.drop_table("literature_hit_translations")
