"""PDF 划线标注 schema（阅读器）。

坐标约定：rects 为归一化到页面的矩形（x0/y0 左上、x1/y1 右下，值域 0..1），
每行一个；前端按当前页宽高还原色块，故缩放无关。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.paper import HIGHLIGHT_COLORS, HIGHLIGHT_STYLES


class Rect(BaseModel):
    x0: float = Field(ge=0, le=1)
    y0: float = Field(ge=0, le=1)
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)


def _norm_color(value: str | None) -> str | None:
    if value is None:
        return None
    return value if value in HIGHLIGHT_COLORS else "yellow"


def _norm_style(value: str | None) -> str | None:
    if value is None:
        return None
    return value if value in HIGHLIGHT_STYLES else "highlight"


class HighlightCreate(BaseModel):
    page: int = Field(ge=1)
    rects: list[Rect] = Field(min_length=1)
    selected_text: str = Field(min_length=1)
    color: str = "yellow"
    style: str = "highlight"
    note: str | None = None

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        return _norm_color(v) or "yellow"

    @field_validator("style")
    @classmethod
    def _style(cls, v: str) -> str:
        return _norm_style(v) or "highlight"


class HighlightUpdate(BaseModel):
    color: str | None = None
    style: str | None = None
    note: str | None = None  # None = 不改；空串 = 清空批注

    @field_validator("color")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        return _norm_color(v)

    @field_validator("style")
    @classmethod
    def _style(cls, v: str | None) -> str | None:
        return _norm_style(v)


class HighlightRead(BaseModel):
    id: uuid.UUID
    paper_id: uuid.UUID
    project_id: uuid.UUID
    author_id: uuid.UUID
    author_name: str  # display_name 回退 email @ 前部分
    page: int
    rects: list[Rect]
    selected_text: str
    color: str
    style: str
    note: str | None
    created_at: datetime
    updated_at: datetime
