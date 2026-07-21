"""model_routes.temperature 可空（None = 不向模型发送该参数）

新款 Claude（如 claude-opus-4-8）已弃用 temperature，请求携带即 400。

Revision ID: c4d5e6f7a8b9
Revises: b3f1a2c9d4e7
Create Date: 2026-07-13
"""

import sqlalchemy as sa

from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b3f1a2c9d4e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_routes") as batch_op:
        batch_op.alter_column(
            "temperature", existing_type=sa.Float(), nullable=True, existing_nullable=False
        )


def downgrade() -> None:
    op.execute("UPDATE model_routes SET temperature = 0.7 WHERE temperature IS NULL")
    with op.batch_alter_table("model_routes") as batch_op:
        batch_op.alter_column(
            "temperature", existing_type=sa.Float(), nullable=False, existing_nullable=True
        )
