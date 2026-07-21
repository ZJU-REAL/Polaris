"""项目 schema。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RubricDimension(BaseModel):
    name: str
    description: str = ""
    weight: float = 1.0


class DefinitionKeywords(BaseModel):
    arxiv_categories: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    synonyms: dict[str, list[str]] = Field(default_factory=dict)


class ProjectDefinition(BaseModel):
    """研究方向定义（docs/api-m1.md §1）。除 statement 外均允许缺省（稀疏草稿）。"""

    statement: str = Field(min_length=1)
    goals: list[str] = Field(default_factory=list)
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    rubric: list[RubricDimension] = Field(default_factory=list)
    anchor_papers: list[dict[str, Any]] = Field(default_factory=list)
    keywords: DefinitionKeywords = Field(default_factory=DefinitionKeywords)
    cadence: str = "daily"


class DraftDefinitionRequest(BaseModel):
    statement: str = Field(min_length=1, max_length=2000)
    name: str | None = Field(default=None, max_length=255)
    keywords_include: list[str] = Field(default_factory=list)


class DraftDefinitionResponse(BaseModel):
    definition: ProjectDefinition
    source: str  # llm | fallback


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
