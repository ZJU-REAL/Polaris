"""研究想法（ideation 产物），带四维评分与 Elo 排位。"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class Idea(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ideas"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    # {"novelty": .., "feasibility": .., "operability": .., "impact": ..}
    scores: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    elo_rating: Mapped[float] = mapped_column(default=1200.0, nullable=False)
    # candidate | under_review | promoted | rejected
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    parent_paper_ids: Mapped[list[Any] | None] = mapped_column(JSONVariant)
