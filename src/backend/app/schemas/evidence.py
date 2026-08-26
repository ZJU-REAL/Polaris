"""阅读器、MCP 和 AI 产物共用的证据定位 DTO。"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


EvidenceAnchorType = Literal["sentence", "paragraph", "chunk", "paper"]
EvidenceFallback = Literal["exact", "sentence", "paragraph", "chunk", "paper"]


class EvidenceAnchorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    paper_id: uuid.UUID
    chunk_id: uuid.UUID | None
    source: str
    content_revision: str
    anchor_key: str
    anchor_type: EvidenceAnchorType
    seq: int | None
    paragraph_index: int | None
    sentence_index: int | None
    quoted_text: str
    locator: dict[str, Any] | None


class EvidenceResolution(BaseModel):
    """解析后的最佳定位；``fallback`` 说明为何没有精确到句子。"""

    model_config = ConfigDict(frozen=True)

    paper_id: uuid.UUID
    anchor_id: uuid.UUID | None
    status: EvidenceFallback
    anchor_type: EvidenceAnchorType
    quoted_text: str
    chunk_id: uuid.UUID | None = None
    seq: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    rects: list[dict[str, float]] = Field(default_factory=list)
    href: str
