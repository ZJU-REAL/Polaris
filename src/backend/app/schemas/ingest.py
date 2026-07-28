"""文献 ingest schema（docs/api-m2.md §4）。"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

#: 检索模式的时间范围 → 回溯**天数**。给固定档而不是让人填数字：这是「最近多久的
#: 论文」这种粗粒度选择，开放输入只会让人纠结 5 个月和 6 个月的差别。
#: 用天而不是月，否则「一周」会被月粒度吃掉。
TIME_RANGE_DAYS: dict[str, int] = {"1w": 7, "3m": 90, "6m": 180, "1y": 365}


class IngestKnobs(BaseModel):
    months_back: int = Field(default=6, ge=1, le=36)  # 检索模式回溯月数（由 time_range 换算）
    max_papers: int = Field(default=150, ge=1, le=500)  # 本次最多检索/打分的篇数（成本上限）
    relevance_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    # 锚点扩展的跳数。每多一跳，Semantic Scholar 的调用量成倍涨，3 跳已经很重
    snowball_depth: int = Field(default=1, ge=0, le=3)
    compile_top_n: int = Field(default=50, ge=1, le=200)  # 打分后精读编译前 N 篇
    # 最大化模式：True 时检索/打分/抽取/编译不设篇数上限（max_papers/compile_top_n 被忽略，
    # 仅保留极高安全哨兵防失控），token 预算也不设限。默认 False，向后兼容。
    unlimited: bool = False


class IngestRequest(BaseModel):
    """启动一次文献收集。

    三种模式，各跑各自需要的步骤：

    - ``search``：按查询词走 arXiv 检索 API，可选时间范围。建库和日后扩充都用它。
    - ``snowball``：从锚点论文出发走引用/参考，跳数可设。
    - ``incremental``：自动模式，从每日论文池里按方向挑（不访问 arXiv）。

    ``bootstrap`` 是 ``search`` 的旧名，保留以兼容存量调用与历史任务。
    """

    mode: Literal["search", "snowball", "incremental", "bootstrap"]
    knobs: IngestKnobs = IngestKnobs()
    #: search 模式的查询词；留空则用库配置里的「包括关键词」
    query_terms: list[str] | None = None
    #: search 模式的时间范围（TIME_RANGES 的键）；给了就覆盖 knobs.months_back
    time_range: Literal["1w", "3m", "6m", "1y"] | None = None


class IngestLastRun(BaseModel):
    voyage_id: uuid.UUID
    status: str
    finished_at: datetime | None
    # 请求者能否打开这个任务的详情页（库可读不等于任务可见）；false 时前端只显示状态、不给跳转
    can_open: bool = False


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
    # 请求者能否打开在跑任务的详情页；false 时前端只显示「运行中」文字、不给跳转
    can_open_running_voyage: bool = False
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
