"""把历史库的起源课题成员补成策展人

库与课题解耦后，文献库工作台改走 /libraries/{id}/* 端点，鉴权口径从
「你是这个库起源课题的成员」变成「你是这个库的策展人，或平台管理员」。
两个口径认的不是同一批人：靠课题成员身份在管库的人，切换后会当场失去
管理权。

这条迁移把每个 project_id 非空的库的起源课题成员，补成该库的策展人，
让切换前后能管这个库的人保持一致。

幂等：已经是策展人的跳过（主键 library_id+user_id 冲突即跳过）。回填的
行记到 _pr3_backfilled_curators，downgrade 只删这批，不碰本来就有的策展人。

Revision ID: b3d81f6c05a9
Revises: a7f4c2e91b83
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3d81f6c05a9"
down_revision: str | None = "a7f4c2e91b83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 备份表：只记这次真正插进去的行，downgrade 据此精确回滚
    op.create_table(
        "_pr3_backfilled_curators",
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("library_id", "user_id"),
    )

    conn = op.get_bind()

    # 待补的（库, 用户）：起源课题的成员，且还不是该库策展人
    missing = list(
        conn.execute(
            sa.text(
                """
                SELECT l.id AS library_id, m.user_id AS user_id
                  FROM direction_libraries l
                  JOIN project_members m ON m.project_id = l.project_id
                 WHERE l.project_id IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM direction_library_curators c
                        WHERE c.library_id = l.id AND c.user_id = m.user_id
                   )
                """
            )
        )
    )
    if not missing:
        return

    rows = [{"library_id": r.library_id, "user_id": r.user_id} for r in missing]
    conn.execute(
        sa.text(
            "INSERT INTO direction_library_curators (library_id, user_id, created_at, updated_at)"
            " VALUES (:library_id, :user_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        rows,
    )
    conn.execute(
        sa.text(
            "INSERT INTO _pr3_backfilled_curators (library_id, user_id)"
            " VALUES (:library_id, :user_id)"
        ),
        rows,
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            DELETE FROM direction_library_curators
             WHERE EXISTS (
                 SELECT 1 FROM _pr3_backfilled_curators b
                  WHERE b.library_id = direction_library_curators.library_id
                    AND b.user_id = direction_library_curators.user_id
             )
            """
        )
    )
    op.drop_table("_pr3_backfilled_curators")
