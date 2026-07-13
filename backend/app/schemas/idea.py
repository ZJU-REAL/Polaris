"""想法 schema。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class IdeaCreate(BaseModel):
    project_id: uuid.UUID
    title: str
    summary: str | None = None
    content: str | None = None
    parent_paper_ids: list[uuid.UUID] | None = None


class IdeaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    summary: str | None
    content: str | None
    scores: dict[str, Any] | None  # novelty/feasibility/operability/impact
    elo_rating: float
    status: str
    parent_paper_ids: list[Any] | None
    created_at: datetime
    updated_at: datetime
