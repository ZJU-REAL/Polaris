"""文献发现领域 DTO 和来源适配器输入输出合同。"""

import uuid
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LiteratureSearchRequest(BaseModel):
    """创建一次检索运行时保存的用户参数。"""

    requested_count: int | None = Field(default=None, ge=1, le=200)
    candidate_budget: int | None = Field(default=None, ge=1, le=1000)
    start_year: int | None = Field(default=None, ge=1800, le=3000)
    end_year: int | None = Field(default=None, ge=1800, le=3000)
    topic: str = Field(min_length=1, max_length=4000)
    query_plan: dict[str, Any] | None = None
    source_config: dict[str, Any] | None = None
    model_version: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_year_window(self) -> "LiteratureSearchRequest":
        if (
            self.start_year is not None
            and self.end_year is not None
            and self.start_year > self.end_year
        ):
            raise ValueError("start_year must not be greater than end_year")
        return self


class LiteratureCandidate(BaseModel):
    """跨来源统一候选字段；来源原始字段放在 metadata 中。"""

    source: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=20_000)
    abstract: str | None = None
    authors: list[dict[str, Any]] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = Field(default=None, max_length=512)
    doi: str | None = Field(default=None, max_length=255)
    pmid: str | None = Field(default=None, max_length=64)
    arxiv_id: str | None = Field(default=None, max_length=64)
    semantic_scholar_id: str | None = Field(default=None, max_length=128)
    url: str | None = Field(default=None, max_length=2048)
    pdf_url: str | None = Field(default=None, max_length=2048)
    oa_status: str | None = Field(default=None, max_length=32)
    citation_count: int | None = Field(default=None, ge=0)
    scores: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class SourceSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    start_year: int | None = Field(default=None, ge=1800, le=3000)
    end_year: int | None = Field(default=None, ge=1800, le=3000)
    limit: int = Field(default=50, ge=1, le=1000)
    cursor: str | None = Field(default=None, max_length=512)


class SourceSearchPage(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    items: list[LiteratureCandidate] = Field(default_factory=list)
    next_cursor: str | None = None
    fetched_count: int = Field(default=0, ge=0)


class SourceAdapter(Protocol):
    """真实来源客户端需要实现的最小异步合同。"""

    name: str

    async def search(self, request: SourceSearchRequest) -> SourceSearchPage:
        ...


class SearchRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    library_id: uuid.UUID
    created_by: uuid.UUID | None
    status: Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
    requested_count: int
    candidate_budget: int
    start_year: int | None
    end_year: int | None
    topic: str
    query_plan: dict[str, Any] | None
    source_config: dict[str, Any] | None
    model_version: str | None
    progress: dict[str, Any] | None
    error_summary: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SourceAttemptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    source: str
    status: Literal["pending", "running", "completed", "partial", "failed", "skipped"]
    query: str | None
    cursor: str | None
    requested_count: int | None
    fetched_count: int
    accepted_count: int
    retryable: bool
    error_code: str | None
    error_detail: str | None
    metadata_snapshot: dict[str, Any] | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SearchHitRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    paper_id: uuid.UUID | None
    status: Literal["candidate", "promoted", "dismissed"]
    source: str
    dedup_key: str
    title: str
    abstract: str | None
    authors: list[dict[str, Any]] | None
    year: int | None
    venue: str | None
    doi: str | None
    pmid: str | None
    arxiv_id: str | None
    semantic_scholar_id: str | None
    url: str | None
    pdf_url: str | None
    oa_status: str | None
    citation_count: int | None
    scores: dict[str, Any] | None
    metadata_snapshot: dict[str, Any] | None
    promoted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SearchHitPage(BaseModel):
    items: list[SearchHitRead]
    total: int
    page: int
    size: int
    sort: str


class OaCacheRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hit_id: uuid.UUID
    status: str
    source_url: str | None
    final_url: str | None
    source: str | None
    blob_id: uuid.UUID | None
    sha256: str | None
    byte_size: int | None
    verification: dict[str, Any] | None
    error_code: str | None
    error_detail: str | None
    attempt_count: int
    downloaded_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OaCacheBatchRequest(BaseModel):
    hit_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class PromoteHitsRequest(BaseModel):
    hit_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class SearchRunDetail(SearchRunRead):
    source_attempts: list[SourceAttemptRead]


class SearchRunPage(BaseModel):
    items: list[SearchRunRead]
    total: int
    page: int
    size: int
