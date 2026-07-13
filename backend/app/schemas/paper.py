"""论文 / 概念 schema。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class PaperCreate(BaseModel):
    project_id: uuid.UUID
    title: str
    arxiv_id: str | None = None
    doi: str | None = None
    authors: list[Any] | None = None
    abstract: str | None = None
    year: int | None = None
    venue: str | None = None
    url: str | None = None


class PaperRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    arxiv_id: str | None
    doi: str | None
    title: str
    authors: list[Any] | None
    abstract: str | None
    year: int | None
    venue: str | None
    url: str | None
    pdf_path: str | None
    relevance_score: float | None
    wiki_content: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class ConceptCreate(BaseModel):
    name: str
    definition: str | None = None
    category: str | None = None


class ConceptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    definition: str | None
    category: str | None
    wiki_content: str | None
    created_at: datetime
    updated_at: datetime
