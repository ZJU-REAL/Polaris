"""项目活动流（agent/人类操作的时间线）。"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class Activity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "activities"

    # P9a：库级活动流。挂课题的活动照旧带 project_id；管理员独立库的 ingest 活动只带
    # library_id（project_id 为空）。隐式库两者都带。
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="SET NULL"), index=True, nullable=True
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=False)  # agent 名或用户 id
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
