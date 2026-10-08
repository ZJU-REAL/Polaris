"""外部 agent（ACP 后端）登记表（#836）

一条记录 = 一个由服务端拉起、经 Agent Client Protocol 对话的外部 agent
（Claude Code、Codex、Gemini CLI……）。env 整体加密，与 mcp_servers 同一约定。

Revision ID: f6b2d8e04a17
Revises: d3f9a1c7e2b4
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f6b2d8e04a17"
down_revision: str | None = "d3f9a1c7e2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "acp_agents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("template", sa.String(length=32), nullable=False, server_default="custom"),
        sa.Column("command", sa.String(length=512), nullable=False),
        sa.Column("args", sa.JSON(), nullable=True),
        sa.Column("env_encrypted", sa.Text(), nullable=True),
        sa.Column("permission_policy", sa.String(length=16), nullable=False, server_default="deny"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_probe", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_probed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("slug", name="uq_acp_agents_slug"),
    )


def downgrade() -> None:
    op.drop_table("acp_agents")
