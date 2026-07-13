"""评审会话 / 消息 schema。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ReviewSessionCreate(BaseModel):
    target_type: str  # idea | manuscript
    target_id: uuid.UUID


class ReviewSessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    status: str
    created_at: datetime
    updated_at: datetime


class ReviewMessageCreate(BaseModel):
    session_id: uuid.UUID
    author_type: str  # agent | human
    author_id: uuid.UUID | None = None
    agent_persona: str | None = None
    content: str
    round: int = 1


class ReviewMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    author_type: str
    author_id: uuid.UUID | None
    agent_persona: str | None
    content: str
    round: int
    created_at: datetime
