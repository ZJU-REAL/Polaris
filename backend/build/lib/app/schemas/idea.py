"""想法 schema（docs/api-m3.md §1/§2/§3）。"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ForgeKnobs(BaseModel):
    num_ideas: int = Field(default=8, ge=1, le=20)  # 生成候选数
    dedup_threshold: float = Field(default=0.85, ge=0.0, le=1.0)  # 余弦相似度阈值
    max_context_papers: int = Field(default=20, ge=1, le=100)  # 知识库上下文论文数上限


class ForgeRequest(BaseModel):
    knobs: ForgeKnobs = ForgeKnobs()


class ForgeLastRun(BaseModel):
    voyage_id: uuid.UUID
    status: str
    finished_at: datetime | None


class IdeaCounts(BaseModel):
    candidate: int = 0
    under_review: int = 0
    promoted: int = 0
    rejected: int = 0
    total: int = 0


class ForgeStateRead(BaseModel):
    running_voyage_id: uuid.UUID | None
    last_run: ForgeLastRun | None
    idea_counts: IdeaCounts


class IdeaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    summary: str | None
    scores: dict[str, Any] | None  # {novelty, feasibility, operability, impact}（0-10）
    elo_rating: float
    status: str  # candidate | under_review | promoted | rejected
    created_at: datetime


class ParentPaperBrief(BaseModel):
    id: uuid.UUID
    title: str


class IdeaDetail(IdeaRead):
    content: str | None  # markdown：动机/方法概述/预期实验/风险
    parent_paper_ids: list[uuid.UUID]
    parent_papers: list[ParentPaperBrief]
    score_rationale: dict[str, Any] | None


class IdeaUpdate(BaseModel):
    status: Literal["rejected"]  # 仅人工淘汰；其他状态转换走专用接口


class IdeaLeaderboardEntry(IdeaRead):
    matches: int
    wins: int
