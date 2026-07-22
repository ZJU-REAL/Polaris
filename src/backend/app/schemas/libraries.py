"""共享方向库 schema（P5c，docs-dev/workspace-ia-redesign.md §2/§6/§7）。

个人文献库（/me/library）的 schema 在 ``app/schemas/library.py``，勿混淆。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DirectionLibrarySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    statement: str | None
    # 过渡期隐式库回指的课题；未来共享库可为 None
    project_id: uuid.UUID | None
    # 是否「我的课题的库」（请求者是背后课题的成员 → 前端显示管理入口）
    is_mine: bool
    paper_count: int
    concept_count: int
    last_compiled_at: datetime | None
    # 上次同步时间（ingest 最近一次跑完的时间，退回同步进度水位）
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DirectionLibraryDetail(DirectionLibrarySummary):
    cadence: str | None
