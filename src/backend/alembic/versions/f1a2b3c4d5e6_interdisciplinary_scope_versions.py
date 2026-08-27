"""Persist immutable versions of interdisciplinary research scopes."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "interdisciplinary_research_profile_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "profile_id",
            sa.Uuid(),
            sa.ForeignKey("interdisciplinary_research_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("research_scope", sa.Text(), nullable=False),
        sa.Column("core_questions", _JSON, nullable=False),
        sa.Column("primary_domain", sa.String(255), nullable=False),
        sa.Column("related_domains", _JSON, nullable=False),
        sa.Column("evidence_boundary", sa.Text()),
        sa.Column("validation_conditions", _JSON),
        sa.Column("user_questions", _JSON),
        sa.Column("query_matrix", _JSON),
        sa.Column("evidence_balance", _JSON),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "confirmed_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("profile_id", "version", name="uq_interdisciplinary_profile_version"),
    )
    op.create_index(
        "ix_interdisciplinary_research_profile_versions_profile_id",
        "interdisciplinary_research_profile_versions",
        ["profile_id"],
    )
    op.create_index(
        "ix_interdisciplinary_research_profile_versions_project_id",
        "interdisciplinary_research_profile_versions",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_interdisciplinary_research_profile_versions_project_id",
        table_name="interdisciplinary_research_profile_versions",
    )
    op.drop_index(
        "ix_interdisciplinary_research_profile_versions_profile_id",
        table_name="interdisciplinary_research_profile_versions",
    )
    op.drop_table("interdisciplinary_research_profile_versions")
