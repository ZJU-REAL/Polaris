"""邮箱验证码：注册验证与找回密码共用一张表。

只存 HMAC 摘要不存明文——库被看到也无法直接冒用；配合尝试次数上限与
重发冷却，抵御暴力猜码。已消费/过期的记录由服务层顺手清理。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class EmailVerificationCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "email_verification_codes"

    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    # register = 注册邮箱验证 | reset = 找回密码
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
