"""provider user agent

Revision ID: 7b3e91c4a2d8
Revises: a1c9e73b5d20
Create Date: 2026-08-10

为 Anthropic 兼容服务提供可选的 Provider 级 User-Agent；空值保持 HTTP 客户端默认行为。
"""

import sqlalchemy as sa

from alembic import op

revision: str = "7b3e91c4a2d8"
down_revision: str | None = "a1c9e73b5d20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_providers", sa.Column("user_agent", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_providers", "user_agent")
