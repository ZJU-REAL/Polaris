"""skills go global: project_skills → user_skills

技能启用不再绑定课题：
- user_skills：用户 × 技能 × 注入点（可 pin 版本），uq(user, skill, target)
- 数据搬运：project_skills 按 created_by 去重（同一用户同技能同注入点
  在多个课题启用过的只保留最早一条），created_by 为空的行丢弃
- skills.project_id 删列（project scope 从未投入使用）
- 降级只重建空的 project_skills（按课题的启用关系无法还原）

Revision ID: 07e7faea4c7a
Revises: c31f7a9d40b2
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "07e7faea4c7a"
down_revision: str | None = "c31f7a9d40b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_skills",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=True),
        sa.Column("target", sa.String(length=64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["skill_versions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "skill_id", "target", name="uq_user_skills_target"),
    )
    op.create_index(op.f("ix_user_skills_user_id"), "user_skills", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_skills_skill_id"), "user_skills", ["skill_id"], unique=False)

    # 同一 (created_by, skill, target) 多课题启用 → 取最早一条；无归属人的行丢弃
    op.execute(
        """
        INSERT INTO user_skills
            (id, user_id, skill_id, version_id, target, config,
             sort_order, enabled, created_at, updated_at)
        SELECT ps.id, ps.created_by, ps.skill_id, ps.version_id, ps.target, ps.config,
               ps.sort_order, ps.enabled, ps.created_at, ps.updated_at
        FROM project_skills ps
        WHERE ps.created_by IS NOT NULL
          AND ps.id = (
              SELECT p2.id FROM project_skills p2
              WHERE p2.created_by = ps.created_by
                AND p2.skill_id = ps.skill_id
                AND p2.target = ps.target
              ORDER BY p2.created_at, p2.id
              LIMIT 1
          )
        """
    )

    op.drop_index(op.f("ix_project_skills_skill_id"), table_name="project_skills")
    op.drop_index(op.f("ix_project_skills_project_id"), table_name="project_skills")
    op.drop_table("project_skills")

    op.drop_index(op.f("ix_skills_project_id"), table_name="skills")
    with op.batch_alter_table("skills") as batch:
        batch.drop_column("project_id")


def downgrade() -> None:
    with op.batch_alter_table("skills") as batch:
        batch.add_column(sa.Column("project_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_skills_project_id"), "skills", ["project_id"], unique=False)

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

    op.drop_index(op.f("ix_user_skills_skill_id"), table_name="user_skills")
    op.drop_index(op.f("ix_user_skills_user_id"), table_name="user_skills")
    op.drop_table("user_skills")
