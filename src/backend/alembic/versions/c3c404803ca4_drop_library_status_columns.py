"""退役 direction_libraries 的审批残留列（status/review_note）

个人化定位（#619）：库的审批流已随 #593/#596 整体移除——新建库即刻可用、
pending/rejected 再无写入口，status 恒为 'active'，review_note 恒空。两列只剩
「看起来还有一套生命周期」的误导性，删掉。共享语义由 is_public 独立承担
（服主部署下创建者可直接设置的「公开给所有人」开关）。

Revision ID: c3c404803ca4
Revises: 9b2e5d81c7a3
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3c404803ca4"
down_revision: str | None = "9b2e5d81c7a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # sqlite 无法原地删列，batch_alter_table 走「重建表」路径；pg 上等价于 DROP COLUMN
    with op.batch_alter_table("direction_libraries") as batch:
        batch.drop_column("review_note")
        batch.drop_column("status")


def downgrade() -> None:
    # 复原 d3a7f1c9b2e4 的形状；数据不可恢复：status 全部落回 server_default 'active'
    # （与「审批流移除后一切库皆 active」的现实一致），review_note 落 NULL。
    with op.batch_alter_table("direction_libraries") as batch:
        batch.add_column(
            sa.Column("status", sa.String(length=16), nullable=False, server_default="active")
        )
        batch.add_column(sa.Column("review_note", sa.Text(), nullable=True))
