"""users.settings 个人设置 JSON 列

- users.settings：用户个人设置（如 chat_fulltext_index 开启文献对话全文索引）；
  与 features（admin 权限位）分开。None/缺键 = 未设置。

Revision ID: 3ecc41527559
Revises: b3e9c1f47a20
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3ecc41527559"
down_revision: str | None = "e2b9d47a0c31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column("users", sa.Column("settings", _JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("users", "settings")
