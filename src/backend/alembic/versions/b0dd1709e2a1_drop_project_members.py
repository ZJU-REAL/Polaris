"""移除课题成员表 project_members（#625）

个人化定位下课题不再共享：归属只看 projects.owner_id（该列自 initial schema
就存在，成员表里的 role=owner 行一直是它的冗余镜像）。可见性/管理判据全部
收敛为 owner_id == user 后，成员表没有读者了，删除。

Revision ID: b0dd1709e2a1
Revises: dd572a7f063c
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b0dd1709e2a1"
down_revision: str | None = "dd572a7f063c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("project_members")


def downgrade() -> None:
    # 复原 54fab61b5e2c 的形状。owner 行可以从 projects.owner_id 反向回填
    # （老代码的可见性判据读的是成员表，不回填会让所有课题凭空消失）；
    # 非 owner 成员行不可恢复——本来就是要删掉的机制。
    op.create_table(
        "project_members",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "user_id"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO project_members (project_id, user_id, role, created_at, updated_at)
            SELECT id, owner_id, 'owner', created_at, updated_at FROM projects
            """
        )
    )
