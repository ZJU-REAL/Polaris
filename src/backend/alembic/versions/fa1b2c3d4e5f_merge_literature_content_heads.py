"""Merge discovery/OA and versioned-content migration heads.

Revision ID: fa1b2c3d4e5f
Revises: d1e2f3a4b5c6, d9e0f1a2b3c4
"""

from collections.abc import Sequence

revision: str = "fa1b2c3d4e5f"
down_revision: str | Sequence[str] | None = ("d1e2f3a4b5c6", "d9e0f1a2b3c4")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
