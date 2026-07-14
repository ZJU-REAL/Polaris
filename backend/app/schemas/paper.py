"""论文 / 概念 / 检索 schema（docs/api-m2.md §1–§3）。"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator


class AuthorRead(BaseModel):
    name: str


def _normalize_authors(value: Any) -> list[dict[str, str]]:
    """兼容历史数据：字符串列表 → [{"name": ...}]。"""
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            normalized.append({"name": item})
        elif isinstance(item, dict) and item.get("name"):
            normalized.append({"name": str(item["name"])})
    return normalized


class PaperRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    authors: list[AuthorRead] = []
    year: int | None
    venue: str | None
    arxiv_id: str | None
    doi: str | None
    url: str | None
    published_at: datetime | None
    relevance_score: float | None
    status: str
    tldr: str | None
    has_wiki: bool = False
    created_at: datetime

    @field_validator("authors", mode="before")
    @classmethod
    def _authors(cls, v: Any) -> list[dict[str, str]]:
        return _normalize_authors(v)


class PaperConceptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    category: str | None


class PaperDetail(PaperRead):
    abstract: str | None
    wiki_content: str | None
    pdf_available: bool = False
    concepts: list[PaperConceptRead] = []


class PaperUpdate(BaseModel):
    """人工纳入/排除。"""

    status: Literal["included", "excluded"] | None = None


class PaperListPage(BaseModel):
    items: list[PaperRead]
    total: int
    page: int
    size: int


class ConceptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    category: str | None
    definition: str | None
    paper_count: int = 0


class ConceptPaperRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    year: int | None


class ConceptRelatedRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class ConceptDetail(ConceptRead):
    wiki_content: str | None
    papers: list[ConceptPaperRead] = []
    related: list[ConceptRelatedRead] = []


class ScoredPaper(PaperRead):
    score: float


class ScoredConcept(ConceptRead):
    score: float


class SearchResponse(BaseModel):
    papers: list[ScoredPaper]
    concepts: list[ScoredConcept]
    mode_used: Literal["keyword", "semantic"]
    reranked: bool = False  # semantic 模式下 rerank 是否成功（失败降级为纯向量分）
