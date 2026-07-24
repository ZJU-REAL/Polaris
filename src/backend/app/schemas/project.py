"""项目 schema。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ProjectCreate(BaseModel):
    """建课题入参（P9c）：只有名称 + 一句话 + 关联哪些已有文献库。

    不再接收收录配置（rubric/anchors/keywords/goals/scope/questions/cadence）——
    那些属于文献库（独立创建、管理员审批）。``statement`` 存入 ``project.statement``
    列（课题语境提示，非收录配置权威）；``source_library_ids`` 关联已有库
    （可为空，空=课题暂无语料）。
    """

    name: str = Field(min_length=1, max_length=255)
    slug: str | None = None  # 缺省时由 name 生成
    statement: str | None = Field(default=None, max_length=2000)
    source_library_ids: list[uuid.UUID] = Field(default_factory=list)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    statement: str | None = Field(default=None, max_length=2000)
    status: str | None = None  # active | archived


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    statement: str | None
    status: str
    owner_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ProjectMemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    email: str | None = None
    display_name: str | None = None


class ProjectDetailRead(ProjectRead):
    members: list[ProjectMemberRead] = []


class ProjectMemberAdd(BaseModel):
    email: EmailStr
    role: str = Field(default="member", pattern="^(member|owner)$")


# ---- 邀请链接 ----


class InviteCreate(BaseModel):
    # 有效天数；None = 永久有效
    expires_days: int | None = Field(default=7, ge=1, le=365)
    # 最大使用次数；None = 不限
    max_uses: int | None = Field(default=None, ge=1, le=1000)


class InviteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    token: str
    expires_at: datetime | None
    max_uses: int | None
    used_count: int
    revoked: bool
    created_at: datetime


class InviteInfo(BaseModel):
    """接受邀请前的预览信息。"""

    project_id: uuid.UUID
    project_name: str
    inviter_name: str | None
    valid: bool
    already_member: bool
