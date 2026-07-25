"""库任务归实验室：回填 library_id，清掉 project_id

建库 / 增量更新是文献库自己的事，课题只是关联库来用语料。但这些任务此前
同时写了 project_id（当时的理由是「兼容活动流与鉴权」），于是它们混在课题
的任务列表里。

两步，顺序不能反：
1. 给还没有 library_id 的库任务回填 —— 「任务库化」改造之前建的那批只挂了
   课题，正是靠 project_id 才找得到自己的库，先清就成了孤儿。解析口径与
   libraries.get_library_for_project 一致：起源库优先，否则取课题关联的第一个库。
2. 清 project_id。此时每个库任务都有 library_id，agents 解析库走的是
   run.library_id 那条分支 —— 独立库的任务本来就是 project_id=NULL 在跑，
   同一条已验证路径。

解析不出库的任务保留 project_id 不动（宁可留在课题里，也不做成没库没课题的
孤儿）。原值记进 _c5e2a90d_voyage_topic 备份表，downgrade 逐行还原。

Revision ID: c5e2a90d41f7
Revises: b3d81f6c05a9
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5e2a90d41f7"
down_revision: str | None = "b3d81f6c05a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# app.models.voyage.LIBRARY_KINDS —— 迁移不 import 应用代码（模型会漂移），这里内联
LIBRARY_KINDS = ("wiki_bootstrap", "wiki_ingest")


def upgrade() -> None:
    op.create_table(
        "_c5e2a90d_voyage_topic",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("backfilled_library", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    conn = op.get_bind()
    kinds = sa.bindparam("kinds", expanding=True)

    # 1. 回填 library_id：起源库优先，否则课题关联的第一个库（关联建立时间序）
    to_backfill = list(
        conn.execute(
            sa.text(
                """
                SELECT v.id AS run_id,
                       v.project_id AS project_id,
                       COALESCE(
                           (SELECT l.id FROM direction_libraries l
                             WHERE l.project_id = v.project_id LIMIT 1),
                           (SELECT t.library_id FROM topic_source_libraries t
                             WHERE t.topic_id = v.project_id
                             ORDER BY t.created_at LIMIT 1)
                       ) AS library_id
                  FROM voyage_runs v
                 WHERE v.library_id IS NULL
                   AND v.project_id IS NOT NULL
                   AND v.kind IN :kinds
                """
            ).bindparams(kinds),
            {"kinds": list(LIBRARY_KINDS)},
        )
    )
    resolved = [r for r in to_backfill if r.library_id is not None]
    if resolved:
        conn.execute(
            sa.text("UPDATE voyage_runs SET library_id = :library_id WHERE id = :run_id"),
            [{"library_id": r.library_id, "run_id": r.run_id} for r in resolved],
        )

    # 2. 清 project_id —— 只清已经有 library_id 的库任务（含刚回填的）
    cleared = list(
        conn.execute(
            sa.text(
                """
                SELECT id AS run_id, project_id
                  FROM voyage_runs
                 WHERE library_id IS NOT NULL
                   AND project_id IS NOT NULL
                   AND kind IN :kinds
                """
            ).bindparams(kinds),
            {"kinds": list(LIBRARY_KINDS)},
        )
    )
    if not cleared:
        return
    backfilled = {r.run_id for r in resolved}
    conn.execute(
        sa.text(
            "INSERT INTO _c5e2a90d_voyage_topic (run_id, project_id, backfilled_library)"
            " VALUES (:run_id, :project_id, :backfilled_library)"
        ),
        [
            {
                "run_id": r.run_id,
                "project_id": r.project_id,
                "backfilled_library": r.run_id in backfilled,
            }
            for r in cleared
        ],
    )
    conn.execute(
        sa.text("UPDATE voyage_runs SET project_id = NULL WHERE id = :run_id"),
        [{"run_id": r.run_id} for r in cleared],
    )


def downgrade() -> None:
    conn = op.get_bind()
    rows = list(conn.execute(sa.text("SELECT * FROM _c5e2a90d_voyage_topic")))
    if rows:
        conn.execute(
            sa.text("UPDATE voyage_runs SET project_id = :project_id WHERE id = :run_id"),
            [{"project_id": r.project_id, "run_id": r.run_id} for r in rows],
        )
        # 回填上去的 library_id 一并撤掉，回到迁移前的形态
        revert = [r for r in rows if r.backfilled_library]
        if revert:
            conn.execute(
                sa.text("UPDATE voyage_runs SET library_id = NULL WHERE id = :run_id"),
                [{"run_id": r.run_id} for r in revert],
            )
    op.drop_table("_c5e2a90d_voyage_topic")
