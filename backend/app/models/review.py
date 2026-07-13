"""评审会话（idea/manuscript 多智能体+人类评审）与评审消息。"""

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class ReviewSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "review_sessions"

    target_type: Mapped[str] = mapped_column(String(32), nullable=False)  # idea | manuscript
    target_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)

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
    agent_persona: Mapped[str | None] = mapped_column(String(64))  # agent 时的评审人设
    content: Mapped[str] = mapped_column(Text, nullable=False)
    round: Mapped[int] = mapped_column(default=1, nullable=False)

    session: Mapped[ReviewSession] = relationship(back_populates="messages")
