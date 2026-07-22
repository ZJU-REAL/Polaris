"""registration_codes.preset_directions：邀请码预设研究方向

用该码注册的新用户会自动获得这些方向的项目（稀疏 definition，仅 statement）。

Revision ID: 94e6bc81c510
Revises: 8b3b904e7588
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "94e6bc81c510"
down_revision: str | None = "8b3b904e7588"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "registration_codes",
        sa.Column("preset_directions", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("registration_codes", "preset_directions")
