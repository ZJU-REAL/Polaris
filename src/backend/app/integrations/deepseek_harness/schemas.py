"""Versioned response contracts consumed by the DeepSeek Harness plugin."""

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_REVISION_PATTERN = r"^[a-f0-9]{64}$"


class HarnessSkillFile(BaseModel):
    path: str = Field(min_length=1)
    size: int = Field(ge=0)
    revision: str = Field(pattern=_REVISION_PATTERN)


class HarnessSkillCatalogItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    invocation: Literal["auto", "manual"]
    scope: Literal["builtin", "user"]
    allowed_tools: list[str] | None = Field(alias="allowedTools")
    files: list[HarnessSkillFile]
    revision: str = Field(pattern=_REVISION_PATTERN)
    updated_at: datetime = Field(alias="updatedAt")

    @field_validator("updated_at")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        """SQLite drops timezone metadata; the external contract always emits UTC."""

        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class HarnessSkillCatalog(BaseModel):
    revision: str
    skills: list[HarnessSkillCatalogItem]


class HarnessSkillDefinition(HarnessSkillCatalogItem):
    body: str
