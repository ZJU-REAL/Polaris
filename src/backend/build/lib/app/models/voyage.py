"""Voyage：长时程 agent 任务的持久化状态机（见 docs/architecture.md §3）。

- VoyageRun：一次航程（目标、计划、游标、检查点、预算/用量）
- VoyageStep：航程中的单个步骤（动作、观测、Sextant 判定、token 记账）
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

# 状态机：planning → executing → verifying → (next|replanning|paused_gate|paused_error)
#         → done / failed / cancelled
VOYAGE_STATUSES = (
    "planning",
    "executing",
    "verifying",
    "replanning",
    "paused_gate",
    "paused_error",
    "done",
    "failed",
    "cancelled",
)
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


class VoyageRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "voyage_runs"

    # demo | ingest | forge | experiment | writing ...
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="planning", index=True, nullable=False)
    # Navigator 产出的步骤列表 [{title, action, params, acceptance?, requires_gate?}, ...]
    plan: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 断点恢复用工作区：artifacts / gates / replans 计数等
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    budget: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)  # {max_tokens?, ...}
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)  # 累计 tokens
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    steps: Mapped[list["VoyageStep"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="VoyageStep.seq",
    )


class VoyageStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "voyage_steps"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_voyage_steps_run_seq"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("voyage_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # actions.py 注册表键
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    observation: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    verdict: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)  # {passed, reason}
    # pending | running | passed | failed | skipped
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    tokens: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[VoyageRun] = relationship(back_populates="steps")
