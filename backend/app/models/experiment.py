"""实验（promoted idea 的验证执行，与 experiment voyage 1:1）与单次运行记录。"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

# 状态与 voyage 流水线联动（docs/api-m4.md §2）：
#   planning →(计划写入) awaiting_gate →(闸门批准) setup → running → reporting → done
#   任一环节失败 → failed；用户取消 → cancelled
EXPERIMENT_STATUSES = (
    "planning",
    "awaiting_gate",
    "setup",
    "running",
    "reporting",
    "done",
    "failed",
    "cancelled",
)
EXPERIMENT_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


class Experiment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "experiments"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    idea_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ideas.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 实验与 voyage 1:1（kind=experiment）
    voyage_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voyage_runs.id", ondelete="SET NULL"), index=True
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ssh_credentials.id", ondelete="SET NULL")
    )
    # {"hypotheses": [{text, status}], "repro_strategy", "steps", "budget_estimate"}
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    status: Mapped[str] = mapped_column(String(32), default="planning", nullable=False)
    workdir: Mapped[str | None] = mapped_column(String(1024))  # ~/polaris_runs/<exp_id>
    server_host: Mapped[str | None] = mapped_column(String(255))
    budget: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)  # {max_hours, max_runs}
    report: Mapped[str | None] = mapped_column(Text)  # markdown 报告
    # {name: [{step, value}]} 全实验汇总（各 run 合并）
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # [{index, name, caption, path 内部}]（docs/api-m5-a.md §2，path 不出 API）
    figures: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONVariant)
    # {no_improve_streak, debug_count, stopped_reason}（iterate 循环持续落库）
    iteration_state: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)

    runs: Mapped[list["ExperimentRun"]] = relationship(
        back_populates="experiment",
        cascade="all, delete-orphan",
        order_by="ExperimentRun.seq",
    )


class ExperimentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "experiment_runs"
    __table_args__ = (UniqueConstraint("experiment_id", "seq", name="uq_experiment_runs_exp_seq"),)

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    # running | succeeded | failed
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    pid: Mapped[int | None] = mapped_column(Integer)  # 远端进程号（cancel 时 kill 用，不出 API）
    log_path: Mapped[str | None] = mapped_column(String(1024))  # 本地日志镜像路径
    # {name: [{step, value}]}（解析 POLARIS_METRIC 行 + 可选 workdir/metrics.json）
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # 该轮 structured reflection（docs/api-m5-a.md §1）
    reflection: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # 主指标值（plan.primary_metric，平台从 metrics 解析）
    primary_value: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    experiment: Mapped[Experiment] = relationship(back_populates="runs")
