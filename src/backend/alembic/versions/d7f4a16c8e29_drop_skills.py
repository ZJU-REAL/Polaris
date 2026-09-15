"""drop the skills feature

Revision ID: d7f4a16c8e29
Revises: c5e02a9b31d7
Create Date: 2026-09-15

技能功能整体移除：插件系统覆盖了它想解决的问题（把可复用的做法交给用户扩展），
而技能自己长成了三套并行（v1 skills/skill_versions/user_skills、v2 agent_skills、
市场 skill_listings），谁也没长全。

**guidance_documents 不在这里**：它是 #741 从 v1 技能表里抽出来的独立小表，
承载跨学科工作流的固定注入点指引——消费模型与技能相反（引擎无条件全文注入，
而不是模型自己决定要不要加载），技能移除后它是那条注入通道的唯一住户，保留。

downgrade 按删除前的**真实形状**重建：列名/约束/索引取自在上一档实跑出来的
schema，不是照着模型手抄。全链降级会继续走回更早的迁移，那些迁移会 UPDATE 这些
表（例如 `skills.is_archived`）——少一列就是 "no such column"，而报错点落在另一个
revision 里，根本看不出根因。数据不可恢复，见 downgrade 注释。
"""

import sqlalchemy as sa

from alembic import op

revision = "d7f4a16c8e29"
down_revision = "c5e02a9b31d7"
branch_labels = None
depends_on = None

#: 删除顺序照顾外键：引用方在前。
_TABLES = (
    "skill_listings",
    "user_skills",
    "skill_versions",
    "skills",
    "agent_skill_files",
    "agent_skills",
)


def upgrade() -> None:
    # CASCADE 是 postgres 的写法，sqlite 直接语法错误（"near CASCADE"）。
    # sqlite 本来也不需要它：按引用方在前的顺序删即可。
    cascade = " CASCADE" if op.get_bind().dialect.name == "postgresql" else ""
    for table in _TABLES:
        op.execute(sa.text(f"DROP TABLE IF EXISTS {table}{cascade}"))


def downgrade() -> None:
    """重建空表，让全链降级走得回去；**内容不可恢复**——技能正文是用户写的。

    真要回退的人应当从备份还原，而不是指望这里。
    """
    op.create_table(
        "skills",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skills_slug", "skills", ["slug"])
    op.create_index("ix_skills_scope", "skills", ["scope"])
    op.create_index("ix_skills_owner_id", "skills", ["owner_id"])

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
    op.create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"])

    op.create_table(
        "user_skills",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=True),
        sa.Column("target", sa.String(length=64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["skill_versions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "skill_id", "target", name="uq_user_skills_target"),
    )
    op.create_index("ix_user_skills_user_id", "user_skills", ["user_id"])
    op.create_index("ix_user_skills_skill_id", "user_skills", ["skill_id"])

    op.create_table(
        "skill_listings",
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("install_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delisted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["skill_version_id"], ["skill_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skill_listings_skill_id", "skill_listings", ["skill_id"])

    op.create_table(
        "agent_skills",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=True),
        sa.Column("invocation", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # 索引清单取自实跑出的 schema：agent_skills 上是 owner_id + scope，没有 slug。
    # 手抄的话，多一个会在降级时 "no such index"，少一个会在更早那档 DROP 时炸。
    op.create_index("ix_agent_skills_owner_id", "agent_skills", ["owner_id"])
    op.create_index("ix_agent_skills_scope", "agent_skills", ["scope"])

    op.create_table(
        "agent_skill_files",
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["skill_id"], ["agent_skills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "path", name="uq_agent_skill_files_path"),
    )
    op.create_index("ix_agent_skill_files_skill_id", "agent_skill_files", ["skill_id"])
