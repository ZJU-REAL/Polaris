"""用户 schema（基于 fastapi-users），注册额外要求邀请码。"""

import uuid
from datetime import datetime
from typing import Any

from fastapi_users import schemas
from pydantic import BaseModel, Field


class UserRead(schemas.BaseUser[uuid.UUID]):
    display_name: str
    role: str
    llm_access: str = "full"
    has_avatar: bool = False
    token_quota: int | None = None
    features: dict[str, Any] | None = None


class UserSearchResult(BaseModel):
    """平台用户查找结果（加协作者用，不含敏感字段）。"""

    id: uuid.UUID
    email: str
    display_name: str


class UserCreate(schemas.BaseUserCreate):
    display_name: str = ""
    invite_code: str  # 与 settings.invite_code 比对，见 api/auth.py

    def create_update_dict(self) -> dict[str, Any]:
        d = super().create_update_dict()
        d.pop("invite_code", None)  # 非表字段，入库前剔除
        return d

    def create_update_dict_superuser(self) -> dict[str, Any]:
        d = super().create_update_dict_superuser()
        d.pop("invite_code", None)
        return d


class UserUpdate(schemas.BaseUserUpdate):
    display_name: str | None = None


# ---- 管理端（/admin/users） ----


class AdminUserRead(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: str
    is_active: bool
    has_avatar: bool
    llm_access: str
    token_quota: int | None
    features: dict[str, Any] | None
    tokens_used: int
    created_at: datetime


class AdminUserUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = Field(default=None, pattern="^(admin|member)$")
    is_active: bool | None = None
    # token_quota：传 -1 表示清除配额（恢复不限）
    token_quota: int | None = Field(default=None, ge=-1)
    features: dict[str, bool] | None = None
    llm_access: str | None = Field(default=None, pattern="^(full|chat_only|blocked)$")


class BatchAssignRequest(BaseModel):
    user_ids: list[uuid.UUID] = Field(min_length=1)
    project_ids: list[uuid.UUID] = Field(min_length=1)
    role: str = Field(default="member", pattern="^(owner|member)$")


class BatchAssignResult(BaseModel):
    added: int  # 新增成员数（已是成员的跳过）


class UsageSummary(BaseModel):
    tokens_used: int
    token_quota: int | None
