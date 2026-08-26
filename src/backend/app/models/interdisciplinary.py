"""Persistent interdisciplinary research profile and confirmation state."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class InterdisciplinaryResearchProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "interdisciplinary_research_profiles"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    research_scope: Mapped[str] = mapped_column(Text, nullable=False)
    core_questions: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False)
    primary_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    related_domains: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False)
    evidence_boundary: Mapped[str | None] = mapped_column(Text)
    validation_conditions: Mapped[list[str] | None] = mapped_column(JSONVariant)
    user_questions: Mapped[list[dict] | None] = mapped_column(JSONVariant)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
