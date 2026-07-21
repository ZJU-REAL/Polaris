"""文献 ingest schema（docs/api-m2.md §4）。"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class IngestKnobs(BaseModel):
    months_back: int = Field(default=6, ge=1, le=36)  # bootstrap 回填月数
    max_papers: int = Field(default=50, ge=1, le=500)  # 本次最多精读编译篇数（成本上限）
    relevance_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    snowball_depth: int = Field(default=1, ge=0, le=2)  # 引文雪球层数
    compile_top_n: int = Field(default=20, ge=1, le=200)  # 打分后精读编译前 N 篇


class IngestRequest(BaseModel):
    mode: Literal["bootstrap", "incremental"]
    knobs: IngestKnobs = IngestKnobs()


class IngestLastRun(BaseModel):
    voyage_id: uuid.UUID
    status: str
    finished_at: datetime | None


class PaperCounts(BaseModel):
    candidate: int = 0
    scored: int = 0
    fetched: int = 0
    compiled: int = 0
    excluded: int = 0
    included: int = 0
    total: int = 0
    # 库内 = 相关性达标及之后（scored+fetched+compiled+included），论文库计数口径
    library: int = 0
    # 待编译 = 达标但还没有 AI 解读（scored+fetched）
    pending_compile: int = 0


class IngestStateRead(BaseModel):
    watermark: str | None
    last_run: IngestLastRun | None
    paper_counts: PaperCounts
    running_voyage_id: uuid.UUID | None
    # 下一次自动同步时间（cadence=daily 且已完成初始建库才有）
    next_sync_at: str | None = None


class ActivityBrief(BaseModel):
    id: uuid.UUID
    kind: str
    message: str
    created_at: datetime


class ProjectStatsRead(BaseModel):
    papers_total: int
    papers_today: int
    ideas_candidate: int
    ideas_under_review: int
    experiments_active: int
    experiments_running: int
    manuscripts_total: int
    manuscripts_under_review: int
    gates_pending: int
    recent_activities: list[ActivityBrief]
