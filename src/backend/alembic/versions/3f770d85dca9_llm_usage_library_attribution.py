"""LLM 用量按方向库归因（P6）

llm_usage / llm_call_logs 加可空 library_id 列（SET NULL）：库侧 ingest
（打分 / 图注 / wiki 编译 / 概念定义 / 向量化）的调用记到方向库账上，
支撑库级月度预算与超限暂停；个人消费仍走 user/project 维度，两本账并行。

Revision ID: 3f770d85dca9
Revises: 1c6c4831d80f
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3f770d85dca9"
down_revision: str | None = "1c6c4831d80f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_usage") as batch:
        batch.add_column(sa.Column("library_id", sa.Uuid(), nullable=True))
        batch.create_index("ix_llm_usage_library_id", ["library_id"])
        batch.create_foreign_key(
            "fk_llm_usage_library_id",
            "direction_libraries",
            ["library_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("llm_call_logs") as batch:
        batch.add_column(sa.Column("library_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_llm_call_logs_library_id",
            "direction_libraries",
            ["library_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_call_logs") as batch:
        batch.drop_constraint("fk_llm_call_logs_library_id", type_="foreignkey")
        batch.drop_column("library_id")
    with op.batch_alter_table("llm_usage") as batch:
        batch.drop_constraint("fk_llm_usage_library_id", type_="foreignkey")
        batch.drop_index("ix_llm_usage_library_id")
        batch.drop_column("library_id")
