"""论文 / 概念 / 检索 / 标签 / AI 伴读 schema（docs/api-m2.md §1–§3、docs/api-lit.md）。"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    affiliations: list[str] = []  # 发表机构（OpenAlex 补充；可能为空）
    year: int | None
    venue: str | None
    arxiv_id: str | None
    doi: str | None
    url: str | None
    published_at: datetime | None
    relevance_score: float | None
    status: str
    # 垃圾桶原因（status=excluded 时有值）：irrelevant 相关性不足 | manual 手动删除
    trash_reason: str | None = None
    tldr: str | None
    has_wiki: bool = False
    created_at: datetime  # 入库时间
    compiled_at: datetime | None = None  # wiki 编译时间；未编译为 null
    # 以下字段不来自 ORM 属性，由 service 层聚合查询后回填（见 papers.paper_extras_map）
    tags: list[str] = []
    starred: bool = False  # 当前用户视角
    reading_status: str = "unread"  # 当前用户视角：unread | reading | read
    note_count: int = 0

    @field_validator("authors", mode="before")
    @classmethod
    def _authors(cls, v: Any) -> list[dict[str, str]]:
        return _normalize_authors(v)

    @field_validator("affiliations", mode="before")
    @classmethod
    def _affiliations(cls, v: Any) -> list[str]:
        return [str(x) for x in v] if isinstance(v, list) else []


class PaperConceptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    category: str | None


class PaperFigure(BaseModel):
    """论文图（docs/api-lit.md §6.5）；图片经 GET /papers/{id}/figures/{index}/image 取。"""

    index: int
    page: int
    width: int
    height: int
    caption: str | None = None
    # motivation | method | architecture | experiment | other；旧数据/未注释为 null
    kind: str | None = None
    important: bool = False


class PaperFiguresResponse(BaseModel):
    figures: list[PaperFigure]


class PaperDetail(PaperRead):
    abstract: str | None
    wiki_content: str | None
    pdf_available: bool = False
    concepts: list[PaperConceptRead] = []
    figures: list[PaperFigure] = []

    @field_validator("figures", mode="before")
    @classmethod
    def _figures(cls, v: Any) -> Any:
        return v or []


class PaperUpdate(BaseModel):
    """人工纳入/排除。"""

    status: Literal["included", "excluded"] | None = None


class PaperListPage(BaseModel):
    items: list[PaperRead]
    total: int
    page: int
    size: int


class PaperManualCreate(BaseModel):
    """手动添加文献：arxiv_id / doi / bibtex 三选一（docs/api-lit.md §4）。"""

    arxiv_id: str | None = None
    doi: str | None = None
    bibtex: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "PaperManualCreate":
        provided = [v for v in (self.arxiv_id, self.doi, self.bibtex) if v and v.strip()]
        if len(provided) != 1:
            raise ValueError("arxiv_id / doi / bibtex 必须且只能填一个")
        return self


class PaperBatchIds(BaseModel):
    """批量操作（删除/导出）的论文 id 列表。"""

    paper_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    # 批量删除：默认软删（移入垃圾桶，可召回）；true = 彻底删除
    hard: bool = False


class PaperTagsUpdate(BaseModel):
    """整组覆盖论文标签；空数组=清空。"""

    names: list[str]


class TagRead(BaseModel):
    id: uuid.UUID
    name: str
    paper_count: int = 0


class PaperMyMetaUpdate(BaseModel):
    """个人状态：星标 / 阅读状态（只更新提供的字段）。"""

    starred: bool | None = None
    reading_status: Literal["unread", "reading", "read"] | None = None


class PaperMyMetaRead(BaseModel):
    starred: bool
    reading_status: str


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class PaperChatRequest(BaseModel):
    """AI 伴读：无状态，历史对话由前端携带（最多最近 10 轮）。"""

    question: str = Field(min_length=1)
    history: list[ChatTurn] = []


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


class ConceptRelinkResult(BaseModel):
    """全库概念补建结果（POST /projects/{id}/concepts/relink）。"""

    papers: int
    concepts_created: int
    links_created: int
    new_concepts: list[str] = []
    # 回填的占位概念数（此前批量截断/失败留下的「定义待补充」重新拿到定义）
    concepts_backfilled: int = 0
    # 同步清理：删除的陈旧关联数 / 删除的零引用概念数（引用计数含回收站论文）
    links_removed: int = 0
    concepts_removed: int = 0


class ScoredPaper(PaperRead):
    score: float


class ScoredConcept(ConceptRead):
    score: float


class SearchResponse(BaseModel):
    papers: list[ScoredPaper]
    concepts: list[ScoredConcept]
    mode_used: Literal["keyword", "semantic"]
    reranked: bool = False  # semantic 模式下 rerank 是否成功（失败降级为纯向量分）
