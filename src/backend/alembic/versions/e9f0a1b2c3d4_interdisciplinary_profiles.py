"""Persist interdisciplinary project profiles and dedicated library markers."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: str | None = "a7c8d9e0f1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("research_mode", sa.String(24), nullable=False, server_default="conventional"),
    )
    with op.batch_alter_table("direction_libraries") as batch:
        batch.add_column(
            sa.Column("library_kind", sa.String(24), nullable=False, server_default="standard")
        )
        batch.add_column(sa.Column("interdisciplinary_domains", _JSON, nullable=True))
        batch.add_column(
            sa.Column(
            "interdisciplinary_project_id",
            sa.Uuid(),
            nullable=True,
            )
        )
        batch.create_foreign_key(
            "fk_direction_libraries_interdisciplinary_project_id",
            "projects",
            ["interdisciplinary_project_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "uq_direction_libraries_interdisciplinary_project",
        "direction_libraries",
        ["interdisciplinary_project_id"],
        unique=True,
        sqlite_where=sa.text("library_kind = 'interdisciplinary'"),
        postgresql_where=sa.text("library_kind = 'interdisciplinary'"),
    )
    op.create_table(
        "interdisciplinary_research_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("research_scope", sa.Text(), nullable=False),
        sa.Column("core_questions", _JSON, nullable=False),
        sa.Column("primary_domain", sa.String(255), nullable=False),
        sa.Column("related_domains", _JSON, nullable=False),
        sa.Column("evidence_boundary", sa.Text(), nullable=True),
        sa.Column("validation_conditions", _JSON, nullable=True),
        sa.Column("user_questions", _JSON, nullable=True),
        sa.Column(
            "created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "confirmed_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_interdisciplinary_research_profiles_project_id",
        "interdisciplinary_research_profiles",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_interdisciplinary_research_profiles_project_id",
        table_name="interdisciplinary_research_profiles",
    )
    op.drop_table("interdisciplinary_research_profiles")
    op.drop_index(
        "uq_direction_libraries_interdisciplinary_project",
        table_name="direction_libraries",
    )
    with op.batch_alter_table("direction_libraries") as batch:
        batch.drop_constraint(
            "fk_direction_libraries_interdisciplinary_project_id", type_="foreignkey"
        )
        batch.drop_column("interdisciplinary_project_id")
        batch.drop_column("interdisciplinary_domains")
        batch.drop_column("library_kind")
    op.drop_column("projects", "research_mode")
