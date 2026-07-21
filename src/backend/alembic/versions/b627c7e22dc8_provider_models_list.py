"""provider models list: llm_providers 增加可用模型列表

- llm_providers：+models（JSON 字符串数组，nullable；None = 未配置）

Revision ID: b627c7e22dc8
Revises: 7a2c9e4f16b3
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b627c7e22dc8"
down_revision: str | None = "7a2c9e4f16b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_providers", sa.Column("models", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_providers", "models")
