"""llm call logs and system settings

- system_settings：系统级 KV 设置表（LLM 调用日志开关等）
- llm_call_logs：LLM 调用日志表（开关打开时记录输入/输出/时延，保留 7 天）

Revision ID: 09533b866a6d
Revises: b627c7e22dc8
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import fastapi_users_db_sqlalchemy.generics
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "09533b866a6d"
down_revision: str | None = "b627c7e22dc8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", _JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "llm_call_logs",
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("provider_name", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("request", _JSON, nullable=True),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("user_id", fastapi_users_db_sqlalchemy.generics.GUID(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("voyage_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["voyage_id"], ["voyage_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_call_logs_created_at", "llm_call_logs", ["created_at"], unique=False)
    op.create_index(op.f("ix_llm_call_logs_stage"), "llm_call_logs", ["stage"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_call_logs_stage"), table_name="llm_call_logs")
    op.drop_index("ix_llm_call_logs_created_at", table_name="llm_call_logs")
    op.drop_table("llm_call_logs")
    op.drop_table("system_settings")
