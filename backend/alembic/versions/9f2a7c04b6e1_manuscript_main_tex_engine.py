"""manuscript main_tex + engine：Overleaf 式可设置编译入口文件与编译器

- manuscripts.main_tex：编译/导出主 .tex（默认 main.tex，官方模板取检测到的主文件）
- manuscripts.engine：编译器 tectonic | pdflatex | xelatex | lualatex（默认 tectonic）

Revision ID: 9f2a7c04b6e1
Revises: a5b6c7d8e9f0
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9f2a7c04b6e1"
down_revision: str | None = "a5b6c7d8e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "manuscripts",
        sa.Column("main_tex", sa.String(length=1024), nullable=False, server_default="main.tex"),
    )
    op.add_column(
        "manuscripts",
        sa.Column("engine", sa.String(length=32), nullable=False, server_default="tectonic"),
    )


def downgrade() -> None:
    op.drop_column("manuscripts", "engine")
    op.drop_column("manuscripts", "main_tex")
