"""合并 LLM 双轨配置（#621）：删除 users.llm_self_managed

自管轨退役：#616 之后平台 providers/routes 人人可配，个人自管配置
（同表里 owner=<user> 的行）失去了存在意义。接管开关随之删除；
存量的 owner=<user> 私有行不动（读取侧一律按 owner IS NULL 过滤），
downgrade 后老代码还能原样接上它们。

Revision ID: dd572a7f063c
Revises: c3c404803ca4
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dd572a7f063c"
down_revision: str | None = "c3c404803ca4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # sqlite 无法原地删列，batch_alter_table 走「重建表」路径；pg 上等价于 DROP COLUMN
    with op.batch_alter_table("users") as batch:
        batch.drop_column("llm_self_managed")


def downgrade() -> None:
    # 复原 a1c7e93f5b02 的形状；谁开过自管已不可考，一律落回默认（被接管）
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("llm_self_managed", sa.Boolean(), nullable=False, server_default=sa.false())
        )
