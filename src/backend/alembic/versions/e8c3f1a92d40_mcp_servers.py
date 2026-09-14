"""外部 MCP 服务器登记表（#754）

一条记录 = 一台可被 agent 调用的外部 MCP 服务器（CAD / 网格 / 求解器 / Blender 等）。

env 整体加密（Fernet(json.dumps(payload))，沿用 connection_credentials 的既有约定）：
stdio 服务器常靠环境变量拿许可证或 API key，那些值和 SSH 凭据同级，不能明文躺在
JSON 列里——备份、日志、误导出都会把它们带走。command/args 刻意不加密：它们是可审计
的运行事实，藏起来只会让「这个 agent 能跑什么」变得不可复核。

Revision ID: e8c3f1a92d40
Revises: d7b2e4c81a35
Create Date: 2026-09-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e8c3f1a92d40"
down_revision: str | None = "d7b2e4c81a35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("transport", sa.String(length=16), nullable=False, server_default="stdio"),
        sa.Column("command", sa.String(length=512), nullable=True),
        sa.Column("args", sa.JSON(), nullable=True),
        sa.Column("cwd", sa.String(length=1024), nullable=True),
        sa.Column("env_encrypted", sa.Text(), nullable=True),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_tools", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("slug", name="uq_mcp_servers_slug"),
    )


def downgrade() -> None:
    op.drop_table("mcp_servers")
