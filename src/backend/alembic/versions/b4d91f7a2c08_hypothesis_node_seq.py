"""hypothesis node creation sequence (#784)

Revision ID: b4d91f7a2c08
Revises: e8c3f1a92d40
Create Date: 2026-09-14

恢复语义（services/hypothesis_tree.best_open_node）承诺「同分取先建」，而此前的排序
键是 ``created_at``——Python 侧 ``utcnow()``，不单调。时钟回拨会让后建的节点拿到更小
的时间戳，同一微秒批量建的兄弟节点更是完全并列。两种情况都不报错，只是这次恢复走进
了另一棵子树。

回填按 ``(created_at, id)`` 排序编号：那正是改之前的最好努力顺序，所以存量数据的读
取顺序一字不变。
"""

import sqlalchemy as sa

from alembic import op

revision = "b4d91f7a2c08"
down_revision = "e8c3f1a92d40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先可空加列再回填再收紧：存量表有行时直接 NOT NULL 会失败
    op.add_column("hypothesis_nodes", sa.Column("seq", sa.Integer(), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, run_id FROM hypothesis_nodes ORDER BY run_id, created_at, id"
        )
    ).fetchall()
    counters: dict[object, int] = {}
    for row in rows:
        run_id = row[1]
        counters[run_id] = counters.get(run_id, 0) + 1
        bind.execute(
            sa.text("UPDATE hypothesis_nodes SET seq = :seq WHERE id = :id"),
            {"seq": counters[run_id], "id": row[0]},
        )

    with op.batch_alter_table("hypothesis_nodes") as batch:
        batch.alter_column("seq", existing_type=sa.Integer(), nullable=False)
    op.create_index(
        "ix_hypothesis_nodes_run_seq", "hypothesis_nodes", ["run_id", "seq"]
    )


def downgrade() -> None:
    op.drop_index("ix_hypothesis_nodes_run_seq", table_name="hypothesis_nodes")
    op.drop_column("hypothesis_nodes", "seq")
