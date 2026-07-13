"""项目（研究方向）与项目成员。"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    definition: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)  # 访谈结果 JSON
    # 文献 ingest 状态：{"watermark": iso, "last_run": {"voyage_id", "finished_at"}}
    # 已处理论文不入此列——去重以 Paper 表（arxiv_id/doi/title）为准
    ingest_state: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectMember(TimestampMixin, Base):
    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), default="member", nullable=False)  # owner|member

    project: Mapped[Project] = relationship(back_populates="members")
