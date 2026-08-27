"""Persist interdisciplinary project profiles and dedicated library markers."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    # SQLite 不支持 ALTER 约束，加带外键的列必须走 batch（仓库既有迁移同此写法）。
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column(
                "research_mode", sa.String(24), nullable=False, server_default="conventional"
            )
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
                sa.ForeignKey(
                    "projects.id",
                    ondelete="SET NULL",
                    name="fk_direction_libraries_interdisciplinary_project",
                ),
                nullable=True,
            )
        )
    op.create_index(
        "ix_direction_libraries_interdisciplinary_project_id",
        "direction_libraries",
        ["interdisciplinary_project_id"],
    )
    # 「一个课题恰好一个跨学科库」由 schema 保证，而不是只靠 confirm 里的先查后插：
    # 并发确认（双击、重试）两边都会查到 None，各建一个库，且外键是 SET NULL，多出来的
    # 那个不会被清掉。
    op.create_index(
        "uq_direction_libraries_interdisciplinary_project",
        "direction_libraries",
        ["interdisciplinary_project_id"],
        unique=True,
        postgresql_where=sa.text("library_kind = 'interdisciplinary'"),
        sqlite_where=sa.text("library_kind = 'interdisciplinary'"),
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
            "created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
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
    op.drop_index(
        "ix_direction_libraries_interdisciplinary_project_id",
        table_name="direction_libraries",
    )
    with op.batch_alter_table("direction_libraries") as batch:
        batch.drop_column("interdisciplinary_project_id")
        batch.drop_column("interdisciplinary_domains")
        batch.drop_column("library_kind")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("research_mode")
