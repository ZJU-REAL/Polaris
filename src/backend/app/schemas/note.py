"""论文笔记 schema（docs/api-lit.md §2）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class NoteCreate(BaseModel):
    content: str = Field(min_length=1)


class NoteUpdate(BaseModel):
    content: str = Field(min_length=1)


class NoteRead(BaseModel):
    id: uuid.UUID
    paper_id: uuid.UUID
    author_id: uuid.UUID
    author_name: str  # display_name 回退 email @ 前部分
    content: str
    created_at: datetime
    updated_at: datetime


class NoteWithPaper(NoteRead):
    paper_title: str


class NotebookPage(BaseModel):
    items: list[NoteWithPaper]
    total: int
    page: int
    size: int
