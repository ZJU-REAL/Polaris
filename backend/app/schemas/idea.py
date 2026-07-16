"""想法 schema（docs/api-m3.md §1/§2/§3 + docs/api-idea2.md）。"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 阶段0 方向发散的信号源（docs/api-idea2.md §1）
FORGE_SIGNALS = ("survey_gap", "concept_holes", "limitations", "trends")


class ForgeKnobs(BaseModel):
    num_ideas: int = Field(default=8, ge=1, le=20)  # 生成候选数
    dedup_threshold: float = Field(default=0.85, ge=0.0, le=1.0)  # 余弦相似度阈值
    max_context_papers: int = Field(default=20, ge=1, le=100)  # 知识库上下文论文数上限
    # 信号源开关（默认全开）
    signals: list[Literal["survey_gap", "concept_holes", "limitations", "trends"]] = Field(
        default_factory=lambda: list(FORGE_SIGNALS)
    )


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


# ---- 深度生成（docs/api-idea2.md §2） ----


class DeepSeed(BaseModel):
    type: Literal["text", "concept", "paper", "idea"]
    value: str = Field(min_length=1, max_length=2000)  # 自由文本 或 concept/paper/idea 的 id


class DeepKnobs(BaseModel):
    confirm_goal: bool = True  # 生成前人工确认研究目标（idea_goal 闸门）
    max_tool_calls: int = Field(default=15, ge=3, le=40)  # 目标构建探索轮数上限
    external_search: bool = True  # 新颖性核查/相关工作是否做外部检索
    revise_rounds: int = Field(default=2, ge=0, le=4)  # 评审-修订最大轮数
    budget_tokens: int | None = Field(default=None, ge=10_000)


class DeepIdeaRequest(BaseModel):
    seed: DeepSeed
    knobs: DeepKnobs = DeepKnobs()


class DeepStateRead(BaseModel):
    running_voyage_id: uuid.UUID | None
    pending_gate_id: uuid.UUID | None
    last_run: ForgeLastRun | None


# ---- Idea 读写 ----


class IdeaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    summary: str | None
    scores: dict[str, Any] | None  # {novelty, feasibility, operability, impact}（0-10）
    elo_rating: float
    status: str  # candidate | under_review | promoted | rejected
    depth: str  # sketch | proposal
    research_type: str | None  # method | benchmark | analysis | survey | application | theory
    created_at: datetime


class ParentPaperBrief(BaseModel):
    id: uuid.UUID
    title: str


class SeedIdeaBrief(BaseModel):
    id: uuid.UUID
    title: str


class IdeaDetail(IdeaRead):
    content: str | None  # sketch：四段式 markdown；proposal：Research Proposal markdown
    parent_paper_ids: list[uuid.UUID]
    parent_papers: list[ParentPaperBrief]
    score_rationale: dict[str, Any] | None
    goal: dict[str, Any] | None  # 结构化研究目标（docs/api-idea2.md §3）
    evidence: list[dict[str, Any]] | None  # [{paper_id?, title, url?, why, source}]
    seed_idea: SeedIdeaBrief | None  # 深化来源草案


class IdeaUpdate(BaseModel):
    status: Literal["rejected"]  # 仅人工淘汰；其他状态转换走专用接口


class IdeaLeaderboardEntry(IdeaRead):
    matches: int
    wins: int
