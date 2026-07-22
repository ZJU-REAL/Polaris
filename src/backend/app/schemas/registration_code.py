"""注册码 API schema。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RegistrationCodeCreate(BaseModel):
    note: str = Field(default="", max_length=255)
    # 有效天数；None = 永久有效
    expires_days: int | None = Field(default=None, ge=1, le=365)
    # 最大使用次数；None = 不限
    max_uses: int | None = Field(default=None, ge=1, le=10000)
    # 预设研究方向：用此码注册的新用户自动获得这些方向的项目
    preset_directions: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("preset_directions")
    @classmethod
    def _clean_directions(cls, v: list[str]) -> list[str]:
        cleaned = [d.strip()[:500] for d in v if d.strip()]
        # 去重（保序）
        return list(dict.fromkeys(cleaned))


class RegistrationCodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    note: str
    expires_at: datetime | None
    max_uses: int | None
    used_count: int
    revoked: bool
    preset_directions: list[str] = Field(default_factory=list)
    # 计算字段：active | revoked | expired | exhausted
    status: str
    created_at: datetime
