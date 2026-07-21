"""个人文献库 schema（issue #108）。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LibraryEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    arxiv_id: str | None
    doi: str | None
    title: str
    authors: list[dict[str, Any]] = []
    year: int | None
    venue: str | None
    abstract: str | None
    url: str | None
    tldr: str | None
    saved: bool
    saved_at: datetime | None
    note: str | None
    visit_count: int
    last_visited_at: datetime | None
    last_paper_id: uuid.UUID | None  # 活体论文软引用；源方向删除后为 null
    created_at: datetime

    @field_validator("authors", mode="before")
    @classmethod
    def _default_authors(cls, v: Any) -> Any:
        return v or []


class LibraryPage(BaseModel):
    items: list[LibraryEntryRead]
    total: int
    page: int
    size: int


class LibraryVisitCreate(BaseModel):
    paper_id: uuid.UUID


class LibrarySaveRequest(BaseModel):
    """二选一：paper_id（从论文收藏）或 entry_id（收藏已有浏览记录）。"""

    paper_id: uuid.UUID | None = None
    entry_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "LibrarySaveRequest":
        if (self.paper_id is None) == (self.entry_id is None):
            raise ValueError("exactly one of paper_id / entry_id is required")
        return self


class LibraryNoteUpdate(BaseModel):
    note: str | None = Field(default=None, max_length=10_000)


class LibraryStateRead(BaseModel):
    """某篇论文在个人库里的状态（阅读页收藏按钮用）。"""

    entry_id: uuid.UUID | None
    saved: bool
