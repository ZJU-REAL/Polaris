"""文献库声明学科：direction_libraries 加 discipline

学科包（YAML 声明抽取 schema）落地后，需要一个地方说明「这个库属于哪个学科」。
它不是展示用的标签，是**抽取口径的选择**：本库论文除内置 schema 外，还按该学科包
的 schema 抽一遍。

为什么必须按库选而不是装上就全局生效：enrich 钩子会遍历 schema 逐个抽，全局生效
意味着装一个结构工程包，连纯机器学习的论文也要多跑一次结构方法卡抽取——代价随
装包数线性上涨，收益为零。为空 = 只用跨学科通用的内置 schema，与本次升级前完全一致。

Revision ID: d7b2e4c81a35
Revises: c4a1d8e93b57
Create Date: 2026-09-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d7b2e4c81a35"
down_revision: str | None = "c4a1d8e93b57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("direction_libraries", sa.Column("discipline", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("direction_libraries", "discipline")
