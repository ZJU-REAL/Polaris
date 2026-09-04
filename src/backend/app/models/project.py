"""项目（研究方向）。

课题成员机制已随个人化定位移除（#625）：课题归创建者（owner_id）所有，
可见性与管理权都只看这一列——服务器档多用户隔离靠它，不再有成员表。
"""

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    # P9e：课题语境提示（一句话）。收录配置（rubric/anchors/keywords/goals/scope/
    # questions/cadence）权威源在文献库 ``DirectionLibrary.definition``，不在课题上。
    statement: Mapped[str | None] = mapped_column(Text)
    # conventional | interdisciplinary; durable project context.
    research_mode: Mapped[str] = mapped_column(
        String(24), default="conventional", server_default="conventional", nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
