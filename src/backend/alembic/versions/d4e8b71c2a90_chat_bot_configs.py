"""add per-user chat bot webhook configs

Revision ID: d4e8b71c2a90
Revises: a9f1c62b70d5
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e8b71c2a90"
down_revision: str | None = "a9f1c62b70d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_bot_configs",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("robot_id_encrypted", sa.Text(), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=True),
        sa.Column("last_delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "platform IN ('dingtalk', 'feishu')", name="ck_chat_bot_configs_platform"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "platform", name="uq_chat_bot_configs_user_platform"
        ),
    )
    op.create_index(
        op.f("ix_chat_bot_configs_user_id"), "chat_bot_configs", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_chat_bot_configs_user_id"), table_name="chat_bot_configs")
    op.drop_table("chat_bot_configs")
