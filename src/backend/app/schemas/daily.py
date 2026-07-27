"""每日新论文池（Daily Paper）schema。"""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.paper import PaperConceptRead


class DailyLiker(BaseModel):
    """点赞人（facepile 头像用）。"""

    id: uuid.UUID
    display_name: str
    has_avatar: bool


class DailyLikerFull(DailyLiker):
    liked_at: datetime


class DailyLikeState(BaseModel):
    """一篇论文的点赞汇总（点/取消赞后返回，供前端乐观更新对账）。"""

    entry_id: uuid.UUID
    like_count: int
    liked_by_me: bool
    likers_preview: list[DailyLiker] = []


class DailyPaperItem(BaseModel):
    entry_id: uuid.UUID
    paper_id: uuid.UUID
    feed_date: date
    primary_category: str
    categories: list[str] = []
    announce_type: Literal["new", "cross"]
    title: str
    authors: list[dict[str, Any]] = []
    # 发表机构（池论文编译后才解析得到；未编译时为空）——前端详情里可点即筛选
    affiliations: list[str] = []
    abstract: str | None
    year: int | None
    arxiv_id: str | None
    url: str | None
    published_at: datetime | None
    has_wiki: bool = False
    like_count: int = 0
    liked_by_me: bool = False
    likers_preview: list[DailyLiker] = []
    # 仅「我赞过的」列表返回
    liked_at: datetime | None = None

    @field_validator("authors", mode="before")
    @classmethod
    def _default_authors(cls, v: Any) -> Any:
        return v or []

    @field_validator("affiliations", mode="before")
    @classmethod
    def _default_affiliations(cls, v: Any) -> list[str]:
        return [str(x) for x in v] if isinstance(v, list) else []


class DailyPaperDetail(DailyPaperItem):
    wiki_content: str | None = None
    pdf_available: bool = False
    # 解读的编译模型 / 时间 / 编译者（都取 paper_wikis 那份；编译徽标 + 覆盖提示用）
    wiki_model: str | None = None
    compiled_at: datetime | None = None
    compiled_by_name: str | None = None
    # 论文已上链的概念（与库版详情同款 chips；池论文无库成员行，概念仍挂在论文上）
    concepts: list[PaperConceptRead] = []


class DailyPage(BaseModel):
    items: list[DailyPaperItem]
    total: int
    page: int
    size: int
    # 实际使用的检索方式：请求 semantic 但数据库/模型不支持时回退 keyword，前端据此提示
    mode_used: Literal["keyword", "semantic"] = "keyword"
    # 语义检索时的向量覆盖度（池内已建向量数 / 池内总数）；池论文默认不建向量，结果可能不全
    vector_ready: int | None = None
    vector_total: int | None = None


class DailyDay(BaseModel):
    date: date
    count: int


class DailyCollectRequest(BaseModel):
    """批量收录：paper_ids × 目标（方向库 / 课题相关研究 / 个人库）。"""

    paper_ids: list[uuid.UUID] = Field(min_length=1)
    direction_library_ids: list[uuid.UUID] = []
    topic_ids: list[uuid.UUID] = []
    personal: bool = False


class DailyCollectResult(BaseModel):
    target_type: Literal["library", "topic", "personal"]
    target_id: uuid.UUID | None
    added: int
    skipped_existing: int
    forbidden: bool


class DailyCollectTask(BaseModel):
    """收录后启动的后台补全任务：paper_id → task_id，供前端订阅分阶段进度（同手动添加）。"""

    paper_id: uuid.UUID
    task_id: str


class DailyCollectResponse(BaseModel):
    results: list[DailyCollectResult]
    # 已启动补全的论文任务（论文已处理完整或 redis 不可用时为空），前端据此弹进度框
    tasks: list[DailyCollectTask] = []


class DailyCollectionsRead(BaseModel):
    """该论文已在哪些收录目标里（树选框预勾选/禁用）。"""

    direction_library_ids: list[uuid.UUID]
    topic_ids: list[uuid.UUID]
    in_personal: bool


class DailyCompileResult(BaseModel):
    """单篇解读编译结果（全实验室共享一份，存 entry 上）。"""

    entry_id: uuid.UUID
    wiki_content: str
    model: str | None


class DailyCategoriesRead(BaseModel):
    categories: list[str]


class DailyCategoriesUpdate(BaseModel):
    categories: list[str] = Field(min_length=1)


class DailySyncStatus(BaseModel):
    """每日论文池的同步健康状况（每日页顶部展示）。"""

    latest_feed_date: str | None = None
    stale: bool = False
    last_run_id: str | None = None
    last_run_status: str | None = None
    last_run_at: str | None = None
    # {分类: {count, status, detail}}
    per_category: dict[str, Any] = Field(default_factory=dict)
    failed_categories: list[str] = Field(default_factory=list)
