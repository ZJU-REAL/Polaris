"""项目（研究方向）与项目成员。"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    # P9c：仅存 {"statement": 一句话}（课题语境提示）。收录配置（rubric/anchors/
    # keywords/goals/scope/questions/cadence）权威源在文献库 definition，不再进此列。
    definition: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # 已退役（P8/P9c）：水位线/last_run 权威源在库 ``DirectionLibrary.ingest_state``。
    # 此列不再被读写，保留仅为暂缓删列（后续迁移可 drop）。
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


class ProjectInvite(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """研究方向邀请链接：持链接的已登录用户可自助加入为成员。"""

    __tablename__ = "project_invites"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # 过期时间；None = 永久有效
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 最大使用次数；None = 不限
    max_uses: Mapped[int | None] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
