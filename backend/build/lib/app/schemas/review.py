"""评审锦标赛 / 会话 / 消息 schema（docs/api-m3.md §3/§4）。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Persona(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    stance: str = Field(min_length=1, max_length=512)


class TournamentRequest(BaseModel):
    idea_ids: list[uuid.UUID] | None = None  # null = 全部 candidate/under_review
    rounds: int = Field(default=2, ge=1, le=5)  # 每对 idea 正/反方各发言轮数
    # null = 默认三人设；顺序约定 [0]=正方 [1]=反方 [2]=裁判
    personas: list[Persona] | None = None


class ReviewSessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    target_type: str  # idea_match | idea_discussion | manuscript
    target_id: uuid.UUID
    status: str
    payload: dict[str, Any] | None
    created_at: datetime


class ReviewMessageCreate(BaseModel):
    content: str = Field(min_length=1)


class ReviewMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    author_type: str  # agent | human
    author_name: str | None  # 人设名或用户 display_name
    content: str
    round: int
    created_at: datetime
