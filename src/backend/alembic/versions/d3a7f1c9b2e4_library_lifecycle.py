"""文献库生命周期：方向库 status/审批（P9b）

- direction_libraries 新增 status（String(16)，NOT NULL，server_default='active'）：
  pending 待审批 | active 已激活（可抓取）| rejected 已驳回。存量库与课题隐式起源库
  经 server_default 回填为 active（不回归）；用户经 POST /libraries 独立建的库落 pending。
- 新增 review_note（Text，驳回理由）+ submitted_by（Uuid，FK→users，SET NULL，库创建者）。

Revision ID: d3a7f1c9b2e4
Revises: b8919453c913
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3a7f1c9b2e4"
down_revision: str | None = "b8919453c913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("direction_libraries") as batch:
        batch.add_column(
            sa.Column("status", sa.String(length=16), nullable=False, server_default="active")
        )
        batch.add_column(sa.Column("review_note", sa.Text(), nullable=True))
        batch.add_column(sa.Column("submitted_by", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_direction_libraries_submitted_by",
            "users",
            ["submitted_by"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("direction_libraries") as batch:
        batch.drop_constraint("fk_direction_libraries_submitted_by", type_="foreignkey")
        batch.drop_column("submitted_by")
        batch.drop_column("review_note")
        batch.drop_column("status")
