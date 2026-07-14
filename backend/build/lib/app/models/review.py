"""评审会话（idea/manuscript 多智能体+人类评审）与评审消息。

target_type（docs/api-m3.md §3/§4）：
- ``idea_match``：一场辩论，payload={"idea_a", "idea_b", "round", "winner"?, "reason"?}，
  target_id 指向正方 idea（idea_a）
- ``idea_discussion``：idea 常驻讨论区（首次 GET sessions 惰性创建），target_id=idea id
- ``manuscript``：M5 稿件评审预留
"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class ReviewSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "review_sessions"

    # idea_match | idea_discussion | manuscript
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)  # open|closed
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)

    messages: Mapped[list["ReviewMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class ReviewMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "review_messages"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    author_type: Mapped[str] = mapped_column(String(16), nullable=False)  # agent | human
    author_id: Mapped[uuid.UUID | None] = mapped_column()  # human 时为 users.id
    author_name: Mapped[str | None] = mapped_column(String(255))  # 人设名或用户 display_name
    content: Mapped[str] = mapped_column(Text, nullable=False)
    round: Mapped[int] = mapped_column(default=1, nullable=False)

    session: Mapped[ReviewSession] = relationship(back_populates="messages")
