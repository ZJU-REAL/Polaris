"""API contracts for project-scoped interdisciplinary research profiles."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class InterdisciplinaryScopeDraft(BaseModel):
    research_scope: str = Field(min_length=10, max_length=4000)
    core_questions: list[str] = Field(min_length=1, max_length=12)
    primary_domain: str = Field(min_length=2, max_length=255)
    related_domains: list[str] = Field(min_length=1, max_length=12)
    evidence_boundary: str | None = Field(default=None, max_length=4000)
    validation_conditions: list[str] | None = Field(default=None, max_length=12)
    user_questions: list[dict] | None = Field(default=None, max_length=12)


class InterdisciplinaryScopeRead(InterdisciplinaryScopeDraft):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    version: int
    status: str
    created_by: uuid.UUID
    confirmed_by: uuid.UUID | None
    confirmed_at: datetime | None


class InterdisciplinaryConfirmation(BaseModel):
    profile: InterdisciplinaryScopeRead
    library_id: uuid.UUID
