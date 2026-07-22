"""课题「相关研究」书架 schema（P5a）。"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ShelfItemRead(BaseModel):
    """书架条目：论文元数据 + 备注 + 解析后的 wiki（库版实时 > 个人版 > 快照）。"""

    paper_id: uuid.UUID
    title: str
    authors: list[dict[str, Any]] = []
    year: int | None
    venue: str | None
    arxiv_id: str | None
    doi: str | None
    url: str | None
    tldr: str | None
    note: str | None
    # live=库版实时可得 | personal=本人个人编译版 | snapshot=只剩入架快照 | none=没有解读
    wiki_source: Literal["live", "personal", "snapshot", "none"]
    wiki_content: str | None
    snapshot_at: datetime | None
    source_library_id: uuid.UUID | None
    added_at: datetime

    @field_validator("authors", mode="before")
    @classmethod
    def _default_authors(cls, v: Any) -> Any:
        return v or []


class ShelfPage(BaseModel):
    items: list[ShelfItemRead]
    total: int
    page: int
    size: int


class ShelfIdsRead(BaseModel):
    """书架全部 paper_id（前端「已入架」勾选态用）。"""

    paper_ids: list[uuid.UUID]


class ShelfAddRequest(BaseModel):
    paper_id: uuid.UUID
    note: str | None = Field(default=None, max_length=10_000)


class ShelfNoteUpdate(BaseModel):
    note: str | None = Field(default=None, max_length=10_000)


class ShelfImportRequest(BaseModel):
    """个人补充入库：arxiv_id / doi / title 至少给一个。"""

    arxiv_id: str | None = None
    doi: str | None = None
    title: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "ShelfImportRequest":
        if not (self.arxiv_id or self.doi or (self.title and self.title.strip())):
            raise ValueError("arxiv_id / doi / title 至少给一个")
        return self
