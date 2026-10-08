"""路由可以指向外部 agent（#840）

model_routes 多一列 acp_agent_id；provider_id 改为可空——一行路由指向模型 API 或
外部 agent，二者恰好其一（服务层保证）。存量行全都有 provider_id，不受影响。

降级时删掉指向 agent 的行（旧版本不认识它们），再把 provider_id 改回非空。

Revision ID: a9c4e7d2f1b8
Revises: f6b2d8e04a17
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9c4e7d2f1b8"
down_revision: str | None = "f6b2d8e04a17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_routes") as batch:
        batch.add_column(sa.Column("acp_agent_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_model_routes_acp_agent_id",
            "acp_agents",
            ["acp_agent_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_model_routes_acp_agent_id", ["acp_agent_id"])
        batch.alter_column("provider_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM model_routes WHERE provider_id IS NULL")
    with op.batch_alter_table("model_routes") as batch:
        batch.alter_column("provider_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_index("ix_model_routes_acp_agent_id")
        batch.drop_constraint("fk_model_routes_acp_agent_id", type_="foreignkey")
        batch.drop_column("acp_agent_id")
