"""per-stage input budgets on model routes

Revision ID: a8d3e5f71c42
Revises: e3a7c91f20b6
Create Date: 2026-09-23

每个环节往 prompt 里放多少材料，以前是代码里的常量（#811）。这一列存路由上的
覆盖值；NULL 表示全用默认，而默认就是原来的常量，所以存量路由升级后行为不变。
"""

import sqlalchemy as sa

from alembic import op

revision = "a8d3e5f71c42"
down_revision = "e3a7c91f20b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_routes") as batch:
        batch.add_column(sa.Column("input_budgets", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_routes") as batch:
        batch.drop_column("input_budgets")
