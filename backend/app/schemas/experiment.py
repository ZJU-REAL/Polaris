"""实验 / 运行 schema。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ExperimentCreate(BaseModel):
    idea_id: uuid.UUID
    plan: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None


class ExperimentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    idea_id: uuid.UUID
    plan: dict[str, Any] | None
    status: str
    workdir: str | None
    server_host: str | None
    budget: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class ExperimentRunCreate(BaseModel):
    experiment_id: uuid.UUID
    command: str


class ExperimentRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    experiment_id: uuid.UUID
    command: str
    log_path: str | None
    metrics: dict[str, Any] | None
    status: str
    created_at: datetime
    updated_at: datetime
