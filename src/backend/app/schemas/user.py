"""用户 schema（基于 fastapi-users），注册额外要求邀请码。"""

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi_users import schemas
from pydantic import BaseModel, EmailStr, Field

USERNAME_PATTERN = r"^[a-z0-9_]{3,32}$"


class UserRead(schemas.BaseUser[uuid.UUID]):
    display_name: str
    username: str | None = None
    username_locked: bool = False
    role: str
    # 只读账号（游客）：前端据此挂只读横幅、把写入入口收起来
    read_only: bool = False
    llm_access: str = "full"
    llm_self_managed: bool = False
    has_avatar: bool = False
    token_quota: int | None = None
    features: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


class UserSearchResult(BaseModel):
    """平台用户查找结果（加协作者用，不含敏感字段）。"""

    id: uuid.UUID
    email: str
    display_name: str


class UserCreate(schemas.BaseUserCreate):
    # 姓名与用户名注册时必填；用户名小写字母/数字/下划线 3-32 位、全局唯一
    display_name: str = Field(min_length=1, max_length=255)
    username: str = Field(pattern=USERNAME_PATTERN)
    invite_code: str  # 与 settings.invite_code 比对，见 api/auth.py
    # 邮箱验证码（先 POST /auth/send-code 获取）；未开启邮件系统时可留空
    email_code: str = ""

    def create_update_dict(self) -> dict[str, Any]:
        d = super().create_update_dict()
        for k in ("invite_code", "email_code"):  # 非表字段，入库前剔除
            d.pop(k, None)
        return d

    def create_update_dict_superuser(self) -> dict[str, Any]:
        d = super().create_update_dict_superuser()
        for k in ("invite_code", "email_code"):
            d.pop(k, None)
        return d


class SendCodeRequest(BaseModel):
    """申请邮箱验证码。"""

    email: EmailStr
    purpose: Literal["register", "reset"]


class SendCodeResult(BaseModel):
    sent: bool
    # 冷却中时的剩余秒数（前端据此倒计时）
    retry_after: int = 0


class ResetPasswordRequest(BaseModel):
    """凭邮箱验证码重设密码。"""

    email: EmailStr
    code: str = Field(min_length=4, max_length=8)
    password: str = Field(min_length=8, max_length=128)


class UserUpdate(schemas.BaseUserUpdate):
    display_name: str | None = None


class UsernameUpdate(BaseModel):
    """本人设置用户名（只能改一次）。"""

    username: str = Field(pattern=USERNAME_PATTERN)


class ManagedCommandWatchdogUserUpdate(BaseModel):
    unanswered_minutes: int = Field(ge=15, le=10_080)


class ManagedCommandWatchdogUserRead(BaseModel):
    unanswered_minutes: int
    admin_max_unanswered_minutes: int
    effective_unanswered_minutes: int


# ---- 管理端（/admin/users） ----


class AdminUserRead(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    username: str | None
    role: str
    read_only: bool = False
    is_active: bool
    has_avatar: bool
    llm_access: str
    llm_self_managed: bool
    token_quota: int | None
    features: dict[str, Any] | None
    tokens_used: int
    created_at: datetime


class AdminUserCreate(BaseModel):
    """管理员直接建号（免邀请码）。"""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    username: str = Field(pattern=USERNAME_PATTERN)
    role: str = Field(default="member", pattern="^(admin|member)$")
    # 游客号：role=admin + read_only=true，看得见管理端、写不动任何东西
    read_only: bool = False
    llm_access: str = Field(default="full", pattern="^(full|chat_only|blocked)$")
    token_quota: int | None = Field(default=None, ge=0)


class AdminUserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    username: str | None = Field(default=None, pattern=USERNAME_PATTERN)
    # 重置密码（≥8 位）；不传 = 不变
    password: str | None = Field(default=None, min_length=8, max_length=128)
    role: str | None = Field(default=None, pattern="^(admin|member)$")
    read_only: bool | None = None
    is_active: bool | None = None
    # token_quota：传 -1 表示清除配额（恢复不限）
    token_quota: int | None = Field(default=None, ge=-1)
    features: dict[str, bool] | None = None
    llm_access: str | None = Field(default=None, pattern="^(full|chat_only|blocked)$")
    # 接管/释放：False=接管（用全局配置）| True=释放（用户自管）
    llm_self_managed: bool | None = None


class BatchAssignRequest(BaseModel):
    user_ids: list[uuid.UUID] = Field(min_length=1)
    project_ids: list[uuid.UUID] = Field(min_length=1)
    role: str = Field(default="member", pattern="^(owner|member)$")


class BatchAssignResult(BaseModel):
    added: int  # 新增成员数（已是成员的跳过）


class BatchDeleteRequest(BaseModel):
    user_ids: list[uuid.UUID] = Field(min_length=1)


class BatchDeleteResult(BaseModel):
    deleted: int


class UsageSummary(BaseModel):
    tokens_used: int
    token_quota: int | None
