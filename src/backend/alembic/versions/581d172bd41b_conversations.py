"""对话服务端持久化 + 三处附带改动

对话历史此前只存在浏览器 localStorage。agent 一轮里可能调好几次工具、中途会断线、
事后要能审计，所以搬到服务端。

顺带三处：
- ``llm_usage.conversation_id``：``voyage_id`` 的天然对偶，让"这场对话花了多少"可回答；
- ``ix_llm_usage_user_id``：user_id 上一直没有索引（project_id/library_id 有），
  agent 循环里要按用户查配额，没索引就是全表扫；
- ``model_routes.context_window``：router 现在不知道模型的上下文有多大，压缩阈值
  只能拍脑袋。

Revision ID: 581d172bd41b
Revises: 78e222c38b3b
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "581d172bd41b"
down_revision: str | None = "78e222c38b3b"
branch_labels: str | None = None
depends_on: str | None = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=True),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("settings", _JSON, nullable=False, server_default="{}"),
        sa.Column("usage", _JSON, nullable=False, server_default="{}"),
        sa.Column("active_stream_id", sa.Uuid(), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index("ix_conversations_project_id", "conversations", ["project_id"])
    op.create_index(
        "ix_conversations_user_scope",
        "conversations",
        ["user_id", "scope_kind", "scope_id", "last_message_at"],
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("blocks", _JSON, nullable=False, server_default="[]"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="complete"),
        sa.Column("sources", _JSON, nullable=True),
        sa.Column("usage", _JSON, nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("stop_reason", sa.String(32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_conversation_messages_seq"),
    )
    op.create_index(
        "ix_conversation_messages_conversation_id", "conversation_messages", ["conversation_id"]
    )

    op.add_column("llm_usage", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.create_index("ix_llm_usage_conversation_id", "llm_usage", ["conversation_id"])
    # user_id 一直没索引，而配额查询按它过滤
    op.create_index("ix_llm_usage_user_id", "llm_usage", ["user_id"])

    op.add_column("model_routes", sa.Column("context_window", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_routes", "context_window")
    op.drop_index("ix_llm_usage_user_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_conversation_id", table_name="llm_usage")
    op.drop_column("llm_usage", "conversation_id")
    op.drop_index("ix_conversation_messages_conversation_id", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversations_user_scope", table_name="conversations")
    op.drop_index("ix_conversations_project_id", table_name="conversations")
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_table("conversations")
