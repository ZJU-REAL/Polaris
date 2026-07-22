"""注册码：管理员生成的可控注册凭证（可设过期时间、最大使用次数、停用）。"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class RegistrationCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """注册码：注册时校验，用尽 / 过期 / 停用即失效。"""

    __tablename__ = "registration_codes"

    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # 备注：这一批码发给谁 / 什么用途
    note: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # 过期时间；None = 永久有效
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 最大使用次数；None = 不限
    max_uses: Mapped[int | None] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 邀请人预设的研究方向（字符串列表）；用此码注册的新用户自动获得这些方向的项目
    preset_directions: Mapped[list[Any] | None] = mapped_column(JSONVariant)
