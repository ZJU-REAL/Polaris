"""持久化的论文证据锚点。

锚点绑定不可变的内容修订哈希；重新解析不会覆盖旧锚点，阅读器解析失败时由服务层
返回句子、段落、分块或论文级回退结果。
"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class PaperEvidenceAnchor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一条可追溯的句子/段落/分块/论文证据位置。"""

    __tablename__ = "paper_evidence_anchors"
    __table_args__ = (
        UniqueConstraint(
            "paper_id",
            "content_revision",
            "anchor_key",
            name="uq_paper_evidence_anchors_revision_key",
        ),
        Index("ix_paper_evidence_anchors_paper_revision", "paper_id", "content_revision"),
        Index("ix_paper_evidence_anchors_chunk", "chunk_id"),
    )

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("paper_content_chunks.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    content_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    anchor_key: Mapped[str] = mapped_column(String(255), nullable=False)
    anchor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    seq: Mapped[int | None] = mapped_column(Integer)
    paragraph_index: Mapped[int | None] = mapped_column(Integer)
    sentence_index: Mapped[int | None] = mapped_column(Integer)
    quoted_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
