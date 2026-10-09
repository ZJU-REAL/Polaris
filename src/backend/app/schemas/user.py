"""用户 schema（读取沿用 fastapi-users 的 BaseUser；单人产品没有注册/改密）。"""

import uuid
from typing import Any

from fastapi_users import schemas
from pydantic import BaseModel, Field

USERNAME_PATTERN = r"^[a-z0-9_]{3,32}$"


class UserRead(schemas.BaseUser[uuid.UUID]):
    display_name: str
    username: str | None = None
    username_locked: bool = False
    has_avatar: bool = False
    settings: dict[str, Any] | None = None
    # 单人产品没有「超管」概念（治理列已随 #614 删除）：fastapi-users 基类自带的
    # is_superuser 不再随 API 下发（exclude 只影响响应序列化；DB 列与内部逻辑不动）。
    # 恒 False 的幽灵字段 llm_self_managed（#621 遗留）已随 #734 重录 golden 移除。
    is_superuser: bool = Field(default=False, exclude=True)


class UserUpdate(BaseModel):
    """PATCH /users/me：本人能改的只有显示名。"""

    display_name: str | None = Field(default=None, max_length=255)


class UsernameUpdate(BaseModel):
    """本人设置用户名（只能改一次）。"""

    username: str = Field(pattern=USERNAME_PATTERN)


class ManagedCommandWatchdogUserUpdate(BaseModel):
    unanswered_minutes: int = Field(ge=15, le=10_080)


class ManagedCommandWatchdogUserRead(BaseModel):
    unanswered_minutes: int
    admin_max_unanswered_minutes: int
    effective_unanswered_minutes: int


class UsageSummary(BaseModel):
    # 只剩用量统计（展示用）；配额上限已随治理机制移除（#614）
    tokens_used: int
