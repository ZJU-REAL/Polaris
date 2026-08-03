"""对话会话与消息。

此前对话历史**只存在浏览器 localStorage**：每次请求前端把最近 10 轮原样 POST 回来，
服务端无状态。这对一问一答够用，对 agent 不够——一轮里模型可能调好几次工具，中途
断线要能续，事后要能审计"它到底查了什么"。

两条设计约束值得写在这里：

1. **``blocks`` 存 provider 中立的块**，不存任何一家的原始 payload。管理端随时能改
   ``ModelRoute``，存了 OpenAI 形状的历史就没法喂给 Anthropic 续跑。
2. **不要对 ``blocks`` 写 JSON path 查询**。测试跑 SQLite、生产跑 Postgres JSONB，
   过滤条件一律走反范式出来的列（``text`` / ``status`` / ``kind``）。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

#: 对话的作用域类型。与前端 chatStorage 的 surfaceKey 一一对应，所以本地历史
#: 迁移到服务端时是直接映射，不用猜。
CONVERSATION_SCOPES = (
    "project",  # 课题文献对话
    "shelf",  # 课题相关研究书架
    "library",  # 单个方向库
    "personal",  # 个人收藏
    "daily",  # 每日论文池
    "paper",  # 单篇伴读
    "global",  # 全局助手（不限作用域）
)


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index(
            "ix_conversations_user_scope",
            "user_id",
            "scope_kind",
            "scope_id",
            "last_message_at",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 用 (scope_kind, scope_id) 而不是六个可空外键：加一个新入口不用改表，而且它就是
    #: 前端 surfaceKey 的形状。代价是拿不到外键级联——但对话是用户资产，课题删了对话
    #: 还留着反而是想要的行为。
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    #: 单独一列：记账与鉴权都按课题走，而 scope_id 在 personal/daily 下是空的
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: {tool_names, effort, stage, ...}
    settings: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    #: 累计 {prompt_tokens, completion_tokens, total_tokens}
    usage: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    #: 有值 = 有一轮正在跑（重连与"别并发发第二轮"都看它）
    active_stream_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_conversation_messages_seq"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    #: normal | compaction（压缩产生的摘要行，UI 折叠显示"已压缩 N 条"）
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    #: 序列化后的 ContentBlock 列表（provider 中立）
    blocks: Mapped[list[Any]] = mapped_column(JSONVariant, nullable=False, default=list)
    #: 拍平的纯文本。反范式出来，列表/标题/搜索都不用碰 JSON——SQLite 上也能查
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="complete")
    #: 该轮引用的论文（ChatSource 列表）。此前它只在 SSE 里飘过一次，从不落库，
    #: 刷新页面就没了。
    sources: Mapped[list[Any] | None] = mapped_column(JSONVariant, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
