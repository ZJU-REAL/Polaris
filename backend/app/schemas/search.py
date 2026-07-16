"""全局搜索 schema（顶栏 ⌘K 跨实体搜索；区别于 wiki 的论文关键词检索）。"""

import uuid
from typing import Literal

from pydantic import BaseModel

GlobalSearchHitType = Literal["paper", "concept", "idea", "experiment", "voyage", "manuscript"]


class GlobalSearchHit(BaseModel):
    type: GlobalSearchHitType
    id: uuid.UUID
    title: str
    snippet: str | None = None
    status: str | None = None


class GlobalSearchResponse(BaseModel):
    query: str
    hits: list[GlobalSearchHit]
