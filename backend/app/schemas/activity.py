"""活动流 schema。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ActivityCreate(BaseModel):
    project_id: uuid.UUID
    actor: str
    kind: str
    message: str
    payload: dict[str, Any] | None = None


class ActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    actor: str
    kind: str
    message: str
    payload: dict[str, Any] | None
    created_at: datetime
