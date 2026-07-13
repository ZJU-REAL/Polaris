"""稿件 / 稿件文件 schema。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ManuscriptCreate(BaseModel):
    project_id: uuid.UUID
    idea_id: uuid.UUID | None = None
    title: str


class ManuscriptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    idea_id: uuid.UUID | None
    title: str
    status: str
    created_at: datetime
    updated_at: datetime


class ManuscriptFileCreate(BaseModel):
    manuscript_id: uuid.UUID
    path: str
    content: str = ""


class ManuscriptFileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    manuscript_id: uuid.UUID
    path: str
    content: str
    created_at: datetime
    updated_at: datetime
