"""项目 schema。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str | None = None  # 缺省时由 name 生成
    definition: dict[str, Any] | None = None


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    definition: dict[str, Any] | None = None
    status: str | None = None  # active | archived


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    definition: dict[str, Any] | None
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
