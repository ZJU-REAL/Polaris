"""列卫生（#734）：退役列删除 + 归属双列合一 + 自管轨存量行清理

各表模型注释里挂了很久的「退役列（代码不再读写；删列待确认稳定后另做迁移）」
在这里一次删掉——解读早已统一到 paper_wikis（a7d0c9e51b34，每篇论文一份），
这些按库/按条目分身的快照列只剩「看起来还有一套并行解读存储」的误导：

- library_papers.wiki_content / compiled_at / compiled_model
- user_library_entries.wiki_content
- daily_feed_entries.wiki_content / wiki_model
- topic_papers.wiki_snapshot / snapshot_at

归属双列合一：direction_libraries 自 P9b 起 submitted_by / created_by 两列并存、
写入时恒等，读点全走 submitted_by。合并进 submitted_by（先回填 NULL 行）后删
created_by；列名保留 submitted_by——30+ 读点与前端类型都在用，改名只有 churn。

自管轨存量行清理（#621 收尾）：llm_providers / model_routes 里 owner_id 非 NULL
的行是自管 LLM 轨时代的私有配置，入口早已拆除，运行时全靠 WHERE owner_id IS NULL
过滤挡着（router / llm_admin）。存量行删除；WHERE 过滤保留为双保险。

llm_usage.conversation_id 本次**不**删：从未有读点、写点也已不存在，列保留一期
（防在途回滚），模型注释已标 deprecated，下一次列卫生迁移删除。

downgrade：列按原形状加回（数据不可恢复，全落 NULL）；created_by 从 submitted_by
回填（两列历史上写入恒等，无损）；被删的自管轨私有行不可恢复（本就无入口可用）。

Revision ID: 351c324f4f6b
Revises: b737c1a2d3e4
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "351c324f4f6b"
# 与 #737（用户偏好搬家 b737c1a2d3e4）并行开发，出货时重挂到它后面成单链。
down_revision: str | None = "b737c1a2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) 归属双列合一：先把 created_by 有值而 submitted_by 空的行回填（防极老存量），
    #    再删副本列。
    op.execute(
        "UPDATE direction_libraries SET submitted_by = created_by "
        "WHERE submitted_by IS NULL AND created_by IS NOT NULL"
    )
    with op.batch_alter_table("direction_libraries") as batch:
        batch.drop_column("created_by")

    # 2) 退役快照列（sqlite 走 batch 重建表；pg 等价 DROP COLUMN）
    with op.batch_alter_table("library_papers") as batch:
        batch.drop_column("wiki_content")
        batch.drop_column("compiled_at")
        batch.drop_column("compiled_model")
    with op.batch_alter_table("user_library_entries") as batch:
        batch.drop_column("wiki_content")
    with op.batch_alter_table("daily_feed_entries") as batch:
        batch.drop_column("wiki_content")
        batch.drop_column("wiki_model")
    with op.batch_alter_table("topic_papers") as batch:
        batch.drop_column("wiki_snapshot")
        batch.drop_column("snapshot_at")

    # 3) 自管轨存量行：先删路由（含挂在私有 provider 下的全局残路由，防悬挂 FK），
    #    再删私有 provider。sqlite 不一定开启 FK 级联，显式两步。
    op.execute(
        "DELETE FROM model_routes WHERE owner_id IS NOT NULL OR provider_id IN "
        "(SELECT id FROM llm_providers WHERE owner_id IS NOT NULL)"
    )
    op.execute("DELETE FROM llm_providers WHERE owner_id IS NOT NULL")


def downgrade() -> None:
    with op.batch_alter_table("topic_papers") as batch:
        batch.add_column(sa.Column("wiki_snapshot", sa.Text(), nullable=True))
        batch.add_column(sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("daily_feed_entries") as batch:
        batch.add_column(sa.Column("wiki_content", sa.Text(), nullable=True))
        batch.add_column(sa.Column("wiki_model", sa.String(length=128), nullable=True))
    with op.batch_alter_table("user_library_entries") as batch:
        batch.add_column(sa.Column("wiki_content", sa.Text(), nullable=True))
    with op.batch_alter_table("library_papers") as batch:
        batch.add_column(sa.Column("wiki_content", sa.Text(), nullable=True))
        batch.add_column(sa.Column("compiled_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("compiled_model", sa.String(length=255), nullable=True))
    with op.batch_alter_table("direction_libraries") as batch:
        batch.add_column(sa.Column("created_by", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_direction_libraries_created_by",
            "users",
            ["created_by"],
            ["id"],
            ondelete="SET NULL",
        )
    # 两列写入历史上恒等：从 submitted_by 回填即无损还原
    op.execute("UPDATE direction_libraries SET created_by = submitted_by")
