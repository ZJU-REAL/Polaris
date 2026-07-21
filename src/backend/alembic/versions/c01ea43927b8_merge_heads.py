"""merge migration heads (voyage_terminal_logs + registration_codes)

两个功能分支的迁移都接在 877da11ed6b4 上，各自合入 main 后形成两个 head，
`alembic upgrade head` 会因多 head 报错。此迁移把两个 head 收敛回单一线性历史，
不做任何 schema 变更。

Revision ID: c01ea43927b8
Revises: d800d34cd207, f7a1c3e59d24
Create Date: 2026-07-21 00:00:00.000000

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "c01ea43927b8"
down_revision: str | Sequence[str] | None = ("d800d34cd207", "f7a1c3e59d24")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
