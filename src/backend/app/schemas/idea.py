"""想法 schema（docs/task-system.md §7）。"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 阶段0 方向发散的信号源（docs/task-system.md §7（原 api-idea2.md §1））
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


class ForgeParkedRun(BaseModel):
    """停下来等人处理的 idea 类任务（出错暂停 / 等回答 / 等确认）。

    这类任务不再算「进行中」（不挡新任务），所以 running_voyage_id 里看不到它；
    单独报出来，想法页才能提醒用户去重试或取消。
    """

    voyage_id: uuid.UUID
    kind: str  # idea_forge | idea_review | idea_proposal ...
    status: str  # paused_error | paused_ask | paused_gate
    # 仅 paused_error：为什么停下（同 VoyageRead.last_error 的 code / message / detail）
    error_code: str | None = None
    error_message: str | None = None
    error_detail: str | None = None
    retry_at: datetime | None = None
    retry_attempt: int = 0


class ForgeStateRead(BaseModel):
    running_voyage_id: uuid.UUID | None
    # 最近一条停着等人的 idea 类任务；没有为 None
    parked_run: ForgeParkedRun | None = None
    last_run: ForgeLastRun | None
    idea_counts: IdeaCounts


# ---- 深度生成（docs/task-system.md §7（原 api-idea2.md §2）） ----


class DeepSeed(BaseModel):
    type: Literal["text", "concept", "paper", "idea"]
    value: str = Field(min_length=1, max_length=2000)  # 自由文本 或 concept/paper/idea 的 id


class DeepKnobs(BaseModel):
    # 生成前人工确认研究目标（idea_goal 闸门）。默认关：单人产品里任务默认直行，
    # 审批断点是显式选项（#626 起闸门机制保留但不再默认拦截）
    confirm_goal: bool = False
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


class EvidenceCounts(BaseModel):
    support: int = 0  # 支持它的论文数
    refute: int = 0  # 反对它的论文数
    unsupported: int = 0  # 没有文献依据的子命题数


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
    trashed_at: datetime | None = None  # 非空即在回收站
    created_at: datetime
    # 写出这个想法的任务（想法生成 / 深度生成 / 假设探索）；任务已删或不是任务写的为空
    source_voyage_id: uuid.UUID | None = None
    # 依据计数，和研究详情里的想法是同一个算法（services.ideas.evidence_counts）。
    # 由 services.ideas.to_read 填；直接 model_validate 得到的是全零
    evidence_counts: EvidenceCounts = Field(default_factory=EvidenceCounts)
    # 来路说明，见 models/idea.py；novelty_scope="library" = 新颖性只对照了文献库
    origin: dict[str, Any] | None = None


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
    goal: dict[str, Any] | None  # 结构化研究目标（docs/task-system.md §7（原 api-idea2.md §3））
    evidence: list[dict[str, Any]] | None  # [{paper_id?, title, url?, why, source}]
    seed_idea: SeedIdeaBrief | None  # 深化来源草案


class IdeaUpdate(BaseModel):
    # 选中（promoted）/ 取消选中（candidate）/ 淘汰（rejected）；under_review 只由评审任务写
    status: Literal["candidate", "promoted", "rejected"]


class IdeaLeaderboardEntry(IdeaRead):
    matches: int
    wins: int
