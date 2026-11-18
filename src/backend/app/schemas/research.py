"""研究（一个方向从输入到想法）的请求与响应。"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.voyage import VoyageMessageRead

# ---- 检索简报 ----


class BriefDraft(BaseModel):
    """模型从一句研究方向推出来的简报（结构化输出的 schema）。

    这里不放枚举和数值范围：学科与来源是磁盘上的数据（装了哪些学科包、哪些来源
    可用），做成 schema 枚举就得每次请求动态拼 schema。取值在拿到之后校验。
    """

    title: str  # 课题名，用研究方向的语言写
    language: Literal["zh", "en"]  # 之后生成的想法和解读用哪种语言
    statement: str  # 3–5 句：问题、对象、方法、什么算相关
    questions: list[str]  # 3–5 个研究问题
    include_keywords: list[str]  # 4–8 个英文检索短语
    exclude_keywords: list[str]
    arxiv_categories: list[str]  # 可以为空
    discipline: str | None  # 已安装的学科包之一，否则为空
    sources: list[str]  # 可选来源的子集
    in_scope: list[str]
    out_of_scope: list[str]
    months_back: int  # 12 | 36 | 60


class ResearchBrief(BriefDraft):
    """存进 research_runs.brief 的简报 = 模型推出来的部分 + 这次检索的规模。

    规模三项不由模型决定：是成本旋钮，默认值在 services/research_runs.py，
    用户在「调整范围」里可以改。
    """

    max_papers: int = Field(default=200, ge=5, le=500)  # 最多检索并筛选多少篇
    compile_top_n: int = Field(default=60, ge=5, le=200)  # 给相关度最高的多少篇写解读
    relevance_threshold: float = Field(default=0.7, ge=0.0, le=1.0)  # 相关度达到多少才留下


class ResearchBriefInput(BaseModel):
    """用户提交的简报：每一项都可以不填。

    填了标题、陈述和关键词（或 arXiv 分类）就原样采用，不再让模型推；只填了部分
    （比如只改检索规模）时，其余由模型推出来，用户填的那几项覆盖上去。
    """

    title: str | None = Field(default=None, max_length=255)
    language: Literal["zh", "en"] | None = None
    statement: str | None = Field(default=None, max_length=4000)
    questions: list[str] | None = None
    include_keywords: list[str] | None = None
    exclude_keywords: list[str] | None = None
    arxiv_categories: list[str] | None = None
    discipline: str | None = None
    sources: list[str] | None = None
    in_scope: list[str] | None = None
    out_of_scope: list[str] | None = None
    months_back: int | None = Field(default=None, ge=1, le=120)
    max_papers: int | None = Field(default=None, ge=5, le=500)
    compile_top_n: int | None = Field(default=None, ge=5, le=200)
    relevance_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


# ---- 请求 ----


class BriefRequest(BaseModel):
    # 长度在接口里校验（1..2000，去掉首尾空白后）：不合格回 422 DIRECTION_INVALID，
    # 界面按这个码出文案
    direction: str


class ResearchCreate(BaseModel):
    direction: str
    client_key: str | None = Field(default=None, min_length=1, max_length=64)
    brief: ResearchBriefInput | None = None


class ResearchRetry(BaseModel):
    #: 改过的简报：给了就按它重新检索文献（任何阶段都回到检索）
    brief: ResearchBriefInput | None = None
    #: 明确重做哪个阶段；不给 = 重试当前阶段
    phase: Literal["survey", "hypotheses"] | None = None
    #: 放宽后重新检索（相关度门槛 0.5、回溯 60 个月）。不给时，只在上次「相关的太少」
    #: 的情况下自动放宽
    broaden: bool | None = None


class MoreIdeasRequest(BaseModel):
    rounds: int | None = Field(default=None, ge=1, le=10)


class ResearchRename(BaseModel):
    name: str = Field(min_length=1, max_length=255)


# ---- 响应 ----


class ResearchAttention(BaseModel):
    """这个阶段为什么没走通。``code`` 稳定，界面按它出文案和唯一的那个操作。"""

    # PLAN_FAILED | LLM_UNREACHABLE | LLM_AUTH | SURVEY_SOURCES_UNREACHABLE |
    # SURVEY_NO_RESULTS | SURVEY_TOO_FEW_RELEVANT | SURVEY_FAILED | LIBRARY_TOO_SMALL |
    # NO_HYPOTHESES_SURVIVED | HYPOTHESES_FAILED
    code: str
    detail: str = ""  # 原始信息，给排查的人看（可能为空）；给用户的句子由界面按 code 出
    # SURVEY_TOO_FEW_RELEVANT: {found, relevant}；LIBRARY_TOO_SMALL: {papers, required}
    counts: dict[str, int] = Field(default_factory=dict)
    # 界面给的那一个操作：retry | edit_scope | broaden | add_papers
    action: str = "retry"


class ResearchCounts(BaseModel):
    papers: int = 0  # 留在文献库里的论文
    ideas: int = 0  # 课题里的想法（不含回收站）
    picked: int = 0  # 其中已选定的


class ResearchRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    library_id: uuid.UUID | None
    name: str  # 课题名
    direction: str
    phase: str  # plan | survey | hypotheses | choose
    status: str  # running | waiting_user | needs_attention | done | cancelled
    attention: ResearchAttention | None = None
    counts: ResearchCounts = Field(default_factory=ResearchCounts)
    created_at: datetime
    updated_at: datetime


class UsageRead(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # 按模型的标价估的费用（美元）：输入 $5 / 百万 token，输出 $25 / 百万 token。
    # 不计缓存折扣，所以是上限的近似
    cost_usd: float = 0.0


class LastErrorRead(BaseModel):
    """当前阶段的任务为什么停下（引擎写的 last_error 里给界面看的那几项）。"""

    code: str
    message: str
    transient: bool = False
    source: str | None = None


class SurveyProgress(BaseModel):
    found: int = 0  # 检索到的
    screened: int = 0  # 已判断过相关度的
    kept: int = 0  # 留下的
    summarised: int = 0  # 已写好解读的
    to_summarise: int = 0  # 这次打算写解读的篇数


class TopPaper(BaseModel):
    id: uuid.UUID
    title: str
    year: int | None = None
    tldr: str | None = None
    relevance: float | None = None


class SourceResult(BaseModel):
    source: str
    fetched: int = 0
    error: str | None = None


class SurveyRead(BaseModel):
    voyage_id: uuid.UUID | None = None
    # 这条任务的状态原样给出（planning / executing / paused_error / done / …）；
    # 还没开始为空。只是提示：阶段到哪了以 phase + status 为准
    state: str | None = None
    step_title: str | None = None  # 正在做哪一步
    started_at: datetime | None = None
    finished_at: datetime | None = None  # 任务结束（完成 / 失败 / 取消）的时刻
    progress: SurveyProgress = Field(default_factory=SurveyProgress)
    top_papers: list[TopPaper] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # 检索的提示（哪个来源失败了等）
    sources: list[SourceResult] = Field(default_factory=list)  # 各来源这次拿到多少
    usage: UsageRead = Field(default_factory=UsageRead)


class HypothesesRead(BaseModel):
    voyage_id: uuid.UUID | None = None
    state: str | None = None  # 同 SurveyRead.state
    step_title: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    round: int = 0  # 已完成几轮
    max_rounds: int = 0
    node_count: int = 0  # 树上的假设数
    usage: UsageRead = Field(default_factory=UsageRead)


class EvidenceCounts(BaseModel):
    support: int = 0  # 支持它的论文数
    refute: int = 0  # 反对它的论文数
    unsupported: int = 0  # 没有文献依据的子命题数


class ResearchIdea(BaseModel):
    id: uuid.UUID
    title: str
    summary: str = ""
    scores: dict[str, Any] | None = None
    status: str  # candidate | under_review | promoted | rejected
    evidence_counts: EvidenceCounts = Field(default_factory=EvidenceCounts)
    origin: dict[str, Any] | None = None
    source_voyage_id: uuid.UUID | None = None
    created_at: datetime


class Capabilities(BaseModel):
    remote_machine: bool = False  # 有没有登记过可以跑实验的远程机器
    latex: bool = False  # 本机能不能把稿件编译成 PDF


class ResearchRunDetail(ResearchRunRead):
    brief: ResearchBrief | None = None
    survey: SurveyRead = Field(default_factory=SurveyRead)
    hypotheses: HypothesesRead = Field(default_factory=HypothesesRead)
    ideas: list[ResearchIdea] = Field(default_factory=list)
    open_ask: VoyageMessageRead | None = None
    next_retry_at: datetime | None = None
    # 即将进行的是第几次自动重试（1..3；0 = 没有自动重试过）
    retry_attempt: int = 0
    # 当前阶段的任务停在出错状态时的原因
    last_error: LastErrorRead | None = None
    # 这次研究到目前为止的用量（各阶段的任务加起来，含重做过的）
    usage: UsageRead = Field(default_factory=UsageRead)
    capabilities: Capabilities = Field(default_factory=Capabilities)
