"""注册码 API schema。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RegistrationCodeCreate(BaseModel):
    note: str = Field(default="", max_length=255)
    # 有效天数；None = 永久有效
    expires_days: int | None = Field(default=None, ge=1, le=365)
    # 最大使用次数；None = 不限
    max_uses: int | None = Field(default=None, ge=1, le=10000)


class RegistrationCodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    note: str
    expires_at: datetime | None
    max_uses: int | None
    used_count: int
    revoked: bool
    # 计算字段：active | revoked | expired | exhausted
    status: str
    created_at: datetime
