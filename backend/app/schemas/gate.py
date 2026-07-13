"""闸门 schema。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class GateCreate(BaseModel):
    project_id: uuid.UUID
    kind: str  # idea_promotion | compute_budget | remote_write | paper_submission
    payload: dict[str, Any] | None = None
    requested_by: str


class GateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    status: str  # pending | approved | rejected
    payload: dict[str, Any] | None
    requested_by: str
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime
