"""add reasoning effort to model routes

Revision ID: 510f6bde2233
Revises: d4e8b71c2a90
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "510f6bde2233"
down_revision: str | None = "d4e8b71c2a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL = 不发送 reasoning_effort / output_config.effort，沿用模型默认档位
    op.add_column("model_routes", sa.Column("effort", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("model_routes", "effort")
