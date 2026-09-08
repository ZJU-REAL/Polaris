"""用户 schema（基于 fastapi-users），注册额外要求邀请码。"""

import uuid
from typing import Any, Literal

from fastapi_users import schemas
from pydantic import BaseModel, EmailStr, Field

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


class UsageSummary(BaseModel):
    # 只剩用量统计（展示用）；配额上限已随治理机制移除（#614）
    tokens_used: int
