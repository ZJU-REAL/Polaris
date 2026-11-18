"""drop mcp_servers: the external MCP server hub is removed

The hub registered external MCP servers (CAD, meshers, solvers) as agent tools. It
never had a screen: rows could only be created by calling the HTTP API by hand, and
nothing re-registered a saved server's tools after a restart. The code is gone, so
the table goes with it.

Polaris's own MCP server (the /mcp endpoint other clients connect to) is unrelated
and unaffected.

Downgrade recreates the empty table; the rows are not restored.

Revision ID: 5e1b7c9a4d32
Revises: d4a8f20c6b19
Create Date: 2026-11-18 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5e1b7c9a4d32"
down_revision: str | None = "d4a8f20c6b19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("mcp_servers")


def downgrade() -> None:
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
