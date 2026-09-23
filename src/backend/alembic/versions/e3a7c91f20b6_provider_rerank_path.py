"""provider rerank path

Revision ID: e3a7c91f20b6
Revises: c1d80a3fb492
Create Date: 2026-09-23

rerank 端点路径此前写死 ``/rerank``（#810）。各家不统一：有的开在 ``/reranks``，于是
本来能用的 reranker 接不进来；而全局改成 ``/reranks`` 会把现在能用的 LiteLLM /
Cohere 风格服务一起弄坏。差异存到 provider 上，由配置的人说了算。

NULL = 用默认值，所以存量 provider 一行不用动，行为一字不变。
"""

import sqlalchemy as sa

from alembic import op

revision = "e3a7c91f20b6"
down_revision = "c1d80a3fb492"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_providers", sa.Column("rerank_path", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_providers", "rerank_path")
