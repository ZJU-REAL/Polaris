"""Buddy 的长期记忆：用户自己写的、希望它一直记得的事。

本期**只有用户能写**。agent 自己往里写属于写工具那一期——一个能自己改自己记忆的
助手，必须和权限模型、审批一起设计，不能顺手做掉。

存正文而不是键值对：记忆是说给模型听的一句话（「我做的是具身智能，别给我推 NLP
的东西」），拆成结构化字段既没必要也会丢掉语气。
"""

import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class BuddyMemory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "buddy_memories"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
