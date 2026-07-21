"""我发表的论文 schema（issue #109）。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AuthorProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name_variants: list[str]
    affiliations: list[str]
    openalex_author_id: str | None
    orcid: str | None
    auto_sync: bool
    last_synced_at: datetime | None


class AuthorProfileUpdate(BaseModel):
    name_variants: list[str] = Field(min_length=1, max_length=20)
    affiliations: list[str] = Field(default_factory=list, max_length=20)
    openalex_author_id: str | None = Field(default=None, max_length=64)
    orcid: str | None = Field(default=None, max_length=32)
    auto_sync: bool = True

    @field_validator("name_variants", "affiliations")
    @classmethod
    def _strip_nonempty(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        return list(dict.fromkeys(cleaned))  # 去重保序


class PublicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    arxiv_id: str | None
    doi: str | None
    title: str
    authors: list[dict[str, Any]] = []
    year: int | None
    venue: str | None
    url: str | None
    cited_by_count: int | None
    paper_id: uuid.UUID | None  # 库内匹配来源的活体论文软引用；论文删除后为 null
    source: str
    status: str
    confirmed_at: datetime | None
    created_at: datetime

    @field_validator("authors", mode="before")
    @classmethod
    def _default_authors(cls, v: Any) -> Any:
        return v or []


class PublicationPage(BaseModel):
    items: list[PublicationRead]
    total: int
    page: int
    size: int
    counts: dict[str, int]  # {pending, confirmed, rejected}（tab 徽标用）


class ManualPublicationCreate(BaseModel):
    """手动补录：arxiv_id | doi | bibtex 三选一。"""

    arxiv_id: str | None = Field(default=None, max_length=64)
    doi: str | None = Field(default=None, max_length=255)
    bibtex: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def _exactly_one(self) -> "ManualPublicationCreate":
        given = [v for v in (self.arxiv_id, self.doi, self.bibtex) if v and v.strip()]
        if len(given) != 1:
            raise ValueError("exactly one of arxiv_id / doi / bibtex is required")
        return self


class SyncEnqueued(BaseModel):
    queued: bool
