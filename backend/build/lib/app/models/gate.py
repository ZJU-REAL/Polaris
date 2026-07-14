"""人工审批闸门：长流程中需人工介入的节点在此落记录并暂停。"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class Gate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "gates"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # idea_promotion | compute_budget | remote_write | paper_submission
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # pending | approved | rejected
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)  # agent 名或用户 id
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)  # 审批意见
