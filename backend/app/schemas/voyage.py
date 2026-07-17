"""Voyage schema（docs/api-m1.md §3）。"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# M1 仅开放 demo 航程；后续里程碑扩展 ingest/forge/experiment/writing
VoyageKind = Literal["demo"]


class VoyageCreate(BaseModel):
    kind: VoyageKind
    project_id: uuid.UUID
    goal: str = Field(min_length=1)
    params: dict[str, Any] | None = None  # 可含 {"budget": {"max_tokens": ...}}


class VoyageStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seq: int  # 创建序（不可变锚点）
    rank: float = 0.0  # 清单序 = 执行序（计划调整插入取间隙值）
    attempt: int = 0  # 尝试次数（>1 = 带诊断重试过）
    title: str
    action: str
    params: dict[str, Any] | None
    observation: dict[str, Any] | None
    verdict: dict[str, Any] | None  # null | {passed, reason}
    status: str
    tokens: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None


class VoyageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    mode: str = "loop"  # pipeline | template | loop（docs/voyage-loop.md §2）
    goal: str
    status: str
    plan_iteration: int = 0  # 计划调整次数
    plan: list[Any] | None
    cursor: int
    budget: dict[str, Any] | None
    usage: dict[str, Any] | None
    project_id: uuid.UUID
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class VoyageSkillUse(BaseModel):
    """本次任务快照中的一个技能（docs/skill-system.md §4.4）。"""

    slug: str
    name: str
    kind: str
    version: int
    target: str


class VoyageDetailRead(VoyageRead):
    steps: list[VoyageStepRead]
    # checkpoint["skills"] 快照摘要（路由填充；无快照为 []）
    skills: list[VoyageSkillUse] = Field(default_factory=list)
