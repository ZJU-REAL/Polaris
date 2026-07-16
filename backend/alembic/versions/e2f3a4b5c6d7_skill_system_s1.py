"""skill system S1 (docs/skill-system.md): skills / skill_versions / project_skills

- skills：技能主体（builtin/user/project scope，slug 唯一性在 service 层按 scope 校验）
- skill_versions：不可变版本（manifest JSON + body），uq(skill, version)
- project_skills：启用到项目（项目 × 技能 × 注入点，可 pin 版本），uq(project, skill, target)

Revision ID: e2f3a4b5c6d7
Revises: c5d6e7f8a9b0
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e2f3a4b5c6d7"
down_revision: str | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("name_en", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_skills_slug"), "skills", ["slug"], unique=False)
    op.create_index(op.f("ix_skills_scope"), "skills", ["scope"], unique=False)
    op.create_index(op.f("ix_skills_owner_id"), "skills", ["owner_id"], unique=False)
    op.create_index(op.f("ix_skills_project_id"), "skills", ["project_id"], unique=False)

    op.create_table(
        "skill_versions",
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_ver"),
    )
    op.create_index(
        op.f("ix_skill_versions_skill_id"), "skill_versions", ["skill_id"], unique=False
    )

    op.create_table(
        "project_skills",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=True),
        sa.Column("target", sa.String(length=64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["skill_versions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "skill_id", "target", name="uq_project_skills_target"),
    )
    op.create_index(
        op.f("ix_project_skills_project_id"), "project_skills", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_project_skills_skill_id"), "project_skills", ["skill_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_project_skills_skill_id"), table_name="project_skills")
    op.drop_index(op.f("ix_project_skills_project_id"), table_name="project_skills")
    op.drop_table("project_skills")
    op.drop_index(op.f("ix_skill_versions_skill_id"), table_name="skill_versions")
    op.drop_table("skill_versions")
    op.drop_index(op.f("ix_skills_project_id"), table_name="skills")
    op.drop_index(op.f("ix_skills_owner_id"), table_name="skills")
    op.drop_index(op.f("ix_skills_scope"), table_name="skills")
    op.drop_index(op.f("ix_skills_slug"), table_name="skills")
    op.drop_table("skills")
