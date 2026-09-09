"""协同文档持久化 CRDT 状态（#347）：manuscript_files 加 ydoc_state

房间此前只从 manuscript_files.content 这一列纯文本重建。重建出来的是一套**新的**
CRDT 历史：服务重启后，仍开着的浏览器标签页带着旧历史自动重连，y-websocket 把
两边合并——两套插入操作互相都不认识，于是整篇文档首尾相接出现两份（issue #347
现场：126 行变 249 行，\\documentclass 同时出现在第 3 行和第 127 行）。

这里加一列存 Y 文档自己的更新字节。有它就按原历史恢复，重连合并回到同一份内容。
纯新增可空列，存量行为 NULL——NULL 时仍按老路子从 content 播种（首次连接、
以及本次升级前就存在的文件），行为不变。

Revision ID: c4a1d8e93b57
Revises: a5b9c3d7e1f2
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4a1d8e93b57"
down_revision: str | None = "a5b9c3d7e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("manuscript_files", sa.Column("ydoc_state", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("manuscript_files", "ydoc_state")
