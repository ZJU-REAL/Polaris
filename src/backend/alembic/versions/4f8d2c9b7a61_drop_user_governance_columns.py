"""移除用户治理列（role/read_only/llm_access/token_quota/features）

个人化定位（#614）：单机档位只有一个用户，机器的主人不需要被自己治理。
角色、只读游客、大模型三档权限、token 配额、功能开关随之整体退役；
avatar_path / llm_self_managed / settings 等个人字段保留。

Revision ID: 4f8d2c9b7a61
Revises: e6c31f84a2d5
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4f8d2c9b7a61"
down_revision: str | None = "e6c31f84a2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # sqlite 无法原地删列，batch_alter_table 走「重建表」路径；pg 上等价于 DROP COLUMN
    with op.batch_alter_table("users") as batch:
        batch.drop_column("role")
        batch.drop_column("read_only")
        batch.drop_column("llm_access")
        batch.drop_column("token_quota")
        batch.drop_column("features")


def downgrade() -> None:
    # 复原 54fab61b5e2c（role）、b3f5c1e07a92（read_only）、b8c9d0e1f2a3（其余三列）
    # 的形状；数据不可恢复，全部落回默认值（member / 非只读 / full / 不限 / 全开）。
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("role", sa.String(length=32), nullable=False, server_default="member")
        )
        batch.add_column(
            sa.Column(
                "read_only", sa.Boolean(), nullable=False, server_default=sa.text("false")
            )
        )
        batch.add_column(
            sa.Column("llm_access", sa.String(length=16), nullable=False, server_default="full")
        )
        batch.add_column(sa.Column("token_quota", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("features", sa.JSON(), nullable=True))
