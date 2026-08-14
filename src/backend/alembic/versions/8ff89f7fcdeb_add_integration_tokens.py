"""add integration tokens

Revision ID: 8ff89f7fcdeb
Revises: 7b3e91c4a2d8
Create Date: 2026-08-13 21:37:41.622536

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8ff89f7fcdeb"
down_revision: str | None = "7b3e91c4a2d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "integration_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("token_prefix", sa.String(24), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("scopes", _JSON, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_integration_tokens_user_id", "integration_tokens", ["user_id"])
    op.create_index(
        "ix_integration_tokens_token_hash", "integration_tokens", ["token_hash"], unique=True
    )
    op.create_index(
        "ix_integration_tokens_user_revoked",
        "integration_tokens",
        ["user_id", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_integration_tokens_user_revoked", table_name="integration_tokens")
    op.drop_index("ix_integration_tokens_token_hash", table_name="integration_tokens")
    op.drop_index("ix_integration_tokens_user_id", table_name="integration_tokens")
    op.drop_table("integration_tokens")
