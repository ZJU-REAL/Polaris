"""Schemas for creating and managing external integration tokens."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IntegrationScope = Literal["skills:read", "mcp:read", "mcp:write"]


class IntegrationTokenCreate(BaseModel):
    """Create a time-limited token; the plaintext is returned once."""

    name: str = Field(min_length=1, max_length=80)
    scopes: list[IntegrationScope] = Field(
        default_factory=lambda: ["skills:read", "mcp:read"], min_length=1, max_length=3
    )
    expires_in_days: int = Field(default=90, ge=1, le=3650)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped

    @field_validator("scopes")
    @classmethod
    def deduplicate_scopes(cls, value: list[IntegrationScope]) -> list[IntegrationScope]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def write_requires_read(self) -> "IntegrationTokenCreate":
        if "mcp:write" in self.scopes and "mcp:read" not in self.scopes:
            raise ValueError("mcp:write requires mcp:read")
        return self


class IntegrationTokenRead(BaseModel):
    """Safe token metadata.  It never contains the bearer secret or digest."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    token_prefix: str
    scopes: list[str]
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime


class IntegrationTokenCreated(IntegrationTokenRead):
    """Creation response containing the one-time plaintext token."""

    token: str
