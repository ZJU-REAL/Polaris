"""每日新论文池（Daily Paper）schema。"""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


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


class DailyPaperDetail(DailyPaperItem):
    wiki_content: str | None = None


class DailyPage(BaseModel):
    items: list[DailyPaperItem]
    total: int
    page: int
    size: int


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
