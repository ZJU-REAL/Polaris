"""实验（idea 的验证计划/执行环境）与单次运行记录。"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class Experiment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "experiments"

    idea_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ideas.id", ondelete="CASCADE"), index=True, nullable=False
    )
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # planning | setup | running | done | failed
    status: Mapped[str] = mapped_column(String(32), default="planning", nullable=False)
    workdir: Mapped[str | None] = mapped_column(String(1024))
    server_host: Mapped[str | None] = mapped_column(String(255))
    budget: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)  # GPU 时长/费用等

    runs: Mapped[list["ExperimentRun"]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan"
    )


class ExperimentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "experiment_runs"

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    command: Mapped[str] = mapped_column(Text, nullable=False)
    log_path: Mapped[str | None] = mapped_column(String(1024))
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)

    experiment: Mapped[Experiment] = relationship(back_populates="runs")
