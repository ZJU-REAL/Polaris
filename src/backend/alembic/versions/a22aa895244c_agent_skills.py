"""Skills v2：SKILL.md 式技能（渐进披露）

与旧的 skills 表并存：那套是"人在写死的注入点上勾选、启动时全文拼进 system prompt"，
这套是"模型自己判断要不要用，常驻的只有一句 description"。voyage 侧继续读旧表，
行为不变；助手只认新表。

Revision ID: a22aa895244c
Revises: 581d172bd41b
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "a22aa895244c"
down_revision: str | None = "581d172bd41b"
branch_labels: str | None = None
depends_on: str | None = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "agent_skills",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("allowed_tools", _JSON, nullable=True),
        sa.Column("invocation", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("scope", sa.String(16), nullable=False, server_default="user"),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("load_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "slug", name="uq_agent_skills_owner_slug"),
    )
    op.create_index("ix_agent_skills_owner_id", "agent_skills", ["owner_id"])
    op.create_index("ix_agent_skills_scope", "agent_skills", ["scope", "enabled"])

    op.create_table(
        "agent_skill_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "skill_id",
            sa.Uuid(),
            sa.ForeignKey("agent_skills.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("skill_id", "path", name="uq_agent_skill_files_path"),
    )
    op.create_index("ix_agent_skill_files_skill_id", "agent_skill_files", ["skill_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_skill_files_skill_id", table_name="agent_skill_files")
    op.drop_table("agent_skill_files")
    op.drop_index("ix_agent_skills_scope", table_name="agent_skills")
    op.drop_index("ix_agent_skills_owner_id", table_name="agent_skills")
    op.drop_table("agent_skills")
