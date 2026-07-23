"""共享方向库 schema（P5c，docs-dev/workspace-ia-redesign.md §2/§6/§7）。

个人文献库（/me/library）的 schema 在 ``app/schemas/library.py``，勿混淆。
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DirectionLibrarySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    statement: str | None
    # 过渡期隐式库回指的课题；未来共享库可为 None
    project_id: uuid.UUID | None
    # 是否「我的课题的库」（请求者是背后课题的成员 → 前端显示管理入口）
    is_mine: bool
    # 是否可管理本库：成员 ∪ 策展人（界面叫「文献库管理员」）∪ 平台 admin（P6）
    can_manage: bool
    paper_count: int
    concept_count: int
    last_compiled_at: datetime | None
    # 上次同步时间（ingest 最近一次跑完的时间，退回同步进度水位）
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DirectionLibraryDetail(DirectionLibrarySummary):
    cadence: str | None
    # 每月 ingest 预算（token 数；None = 不限）
    monthly_budget: int | None = None


class DirectionLibraryUpdate(BaseModel):
    """库定义编辑（PATCH /libraries/{id}）：显式传 null 可清空对应字段。"""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    statement: str | None = None
    cadence: str | None = Field(default=None, max_length=32)
    monthly_budget: int | None = Field(default=None, ge=0)
    rubric: Any | None = None
    anchors: list[Any] | None = None


class CuratorRead(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None


class CuratorsUpdate(BaseModel):
    """策展人名单全量替换（平台 admin）。"""

    user_ids: list[uuid.UUID]


class LibraryBudgetRead(BaseModel):
    """库预算面板（P6）：本月消耗（UTC 自然月，token 口径与 LLMUsage 记账一致）。"""

    month: str  # 如 "2026-07"
    monthly_budget: int | None  # None = 不限
    prompt_tokens: int
    completion_tokens: int
    used_tokens: int
    remaining_tokens: int | None  # 不限时为 None；用超时为 0
    exhausted: bool  # True = 本月预算已用尽（ingest 会被拒绝启动）
