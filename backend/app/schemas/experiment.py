"""实验 / 运行 schema（docs/api-m4.md §2）。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExperimentBudget(BaseModel):
    max_hours: float = Field(default=4, ge=0)
    max_runs: int = Field(default=10, ge=1)


class ExperimentParams(BaseModel):
    gpu_hint: str | None = None
    budget: ExperimentBudget | None = None


class ExperimentCreate(BaseModel):
    idea_id: uuid.UUID
    credential_id: uuid.UUID
    params: ExperimentParams | None = None


class ExperimentRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    idea_id: uuid.UUID
    idea_title: str
    # planning | awaiting_gate | setup | running | reporting | done | failed | cancelled
    status: str
    voyage_id: uuid.UUID | None
    workdir: str | None
    server_host: str | None
    budget: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class ExperimentRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seq: int
    command: str
    status: str  # running | succeeded | failed
    exit_code: int | None
    log_path: str | None
    metrics: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None


class ExperimentDetail(ExperimentRead):
    plan: dict[str, Any] | None
    runs: list[ExperimentRunRead]
    report: str | None
    metrics: dict[str, Any] | None


class ExperimentLogsRead(BaseModel):
    lines: list[str]
    truncated: bool
