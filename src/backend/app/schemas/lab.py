"""实验室数据面板 schema（/lab 概况页）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class LabLibraryStats(BaseModel):
    total: int
    public: int
    personal: int


class LabPaperStats(BaseModel):
    """pool_total 是全局内容池；library_members_deduped 是可见库并集内去重后的篇数。"""

    pool_total: int
    library_members_deduped: int
    compiled: int


class LabConceptStats(BaseModel):
    total: int


class LabChunkStats(BaseModel):
    """全文分段索引：vector_search_supported=false 时向量存了也搜不了（sqlite）。"""

    papers_with_chunks: int
    total_chunks: int
    chunks_with_embedding: int
    vector_search_supported: bool


class LabVectorStats(BaseModel):
    papers_with_embedding: int
    papers_total: int


class LabStats(BaseModel):
    libraries: LabLibraryStats
    papers: LabPaperStats
    concepts: LabConceptStats
    chunks: LabChunkStats
    vectors: LabVectorStats
    # 排行榜对普通用户是否可见（关着时普通用户不显示这一区）
    leaderboard_enabled: bool


class LabLeaderboardEntry(BaseModel):
    user_id: uuid.UUID
    display_name: str | None
    username: str | None
    has_avatar: bool
    role: str
    tokens_used: int


class LabLeaderboardResponse(BaseModel):
    enabled: bool
    days: int
    items: list[LabLeaderboardEntry]


class DailyIngestStatusRow(BaseModel):
    """某个库最近一轮「从每日论文自动收录」的漏斗与状态。"""

    library_id: uuid.UUID
    library_name: str
    is_public: bool
    voyage_id: uuid.UUID
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    # 池子里扫了多少 → 送进打分多少 → 收下多少 / 淘汰多少 → 编译了多少
    feed_total: int = 0
    candidates: int = 0
    admitted: int = 0
    rejected: int = 0
    compiled: int = 0
    step_status: dict[str, str] = {}
