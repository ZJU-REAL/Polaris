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
    # 结构化验收 {text: str|None, checks: [...]|None}：这一步"怎样算通过"
    acceptance: dict[str, Any] | None = None
    # 闸门类型（需要人工审批的步骤）
    requires_gate: str | None = None
    # 溯源 {plan_iteration, ...}：第几次计划调整创建了它（0 = 初始计划）
    provenance: dict[str, Any] | None = None
    observation: dict[str, Any] | None
    verdict: dict[str, Any] | None  # null | {passed, reason}
    status: str
    tokens: dict[str, Any] | None
    # 每次尝试的完整归档 [{attempt, observation, verdict, tokens, started_at, finished_at}]
    attempts: list[Any] | None = None
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
    project_id: uuid.UUID | None = None
    library_id: uuid.UUID | None = None
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


class VoyagePlanEvent(BaseModel):
    """一次计划调整的留痕（checkpoint["plan_history"]，engine 记录）。"""

    iteration: int  # 调整后的 plan_iteration
    source: str  # signal（执行结果规则分支）| navigator（AI 调整）| template（模板分支）
    reason: str  # 调整原因（大白话）
    added: int = 0  # 新增步骤数
    obsoleted: int = 0  # 作废步骤数
    trigger_step: str | None = None  # 触发调整的步骤标题
    at: datetime | None = None


class VoyageTerminalLogRead(BaseModel):
    """任务终端历史日志的一条：结构化日志行（event=log）或大模型完整输出（event=llm）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int  # 自增即时间序，前端据此排序 / 去重
    event: Literal["log", "llm"]
    level: str | None = None  # log 上色 level（info/step/success/error/plan/budget/gate）
    stage: str | None = None  # llm 环节（navigator/librarian/...）
    message: str
    at: datetime


class VoyageDetailRead(VoyageRead):
    steps: list[VoyageStepRead]
    # checkpoint["skills"] 快照摘要（路由填充；无快照为 []）
    skills: list[VoyageSkillUse] = Field(default_factory=list)
    # 计划调整历史（路由从 checkpoint["plan_history"] 填充；无调整为 []）
    plan_history: list[VoyagePlanEvent] = Field(default_factory=list)
