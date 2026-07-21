"""Voyage：长时程 agent 任务的持久化状态机（见 docs/architecture.md §3）。

- VoyageRun：一次航程（目标、计划、游标、检查点、预算/用量）
- VoyageStep：航程中的单个步骤（动作、观测、Sextant 判定、token 记账）
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin, utcnow

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

# ---- 运行模式（docs/voyage-loop.md §2/§3）：由 kind 静态决定，不暴露给用户/LLM 选择 ----
# pipeline：固定计划 + 机械校验，失败不经 LLM 重规划；
# template：固定骨架 + 确定性重规划分支表（LLM 兜底）；
# loop    ：完整 plan-execute-verify 循环——experiment（模板起步 + plan_signal 分支表推进 +
#           失败回灌计划调整）、demo 及 LLM 自由规划的 kind。
PIPELINE_KINDS = frozenset(
    {
        "wiki_bootstrap",
        "wiki_ingest",
        "idea_forge",
        "idea_review",
        "paper_writing",
        "paper_review",
        "presentation",
    }
)
TEMPLATE_KINDS = frozenset({"idea_proposal"})


def mode_for_kind(kind: str) -> str:
    if kind in PIPELINE_KINDS:
        return "pipeline"
    if kind in TEMPLATE_KINDS:
        return "template"
    return "loop"


# 步骤终态：passed 正常推进；obsolete 被计划调整作废（留痕不删除）
STEP_TERMINAL_STATUSES = frozenset({"passed", "obsolete"})


class VoyageRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "voyage_runs"

    # demo | ingest | forge | experiment | writing ...
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # pipeline | template | loop（docs/voyage-loop.md §2，由 kind 派生）
    mode: Mapped[str] = mapped_column(String(16), default="loop", nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="planning", index=True, nullable=False)
    # 当前计划快照（由步骤行单向派生，兼容 API/前端展示；真源是 voyage_steps）
    plan: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 计划演化版本号：每次重规划/计划编辑 +1
    plan_iteration: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # voyage 级完成标准 {checks: [...]}（docs/voyage-loop.md §5.4；None = 步骤走完即 done）
    done_criteria: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
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
        order_by="VoyageStep.rank, VoyageStep.seq",
    )


class VoyageStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "voyage_steps"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_voyage_steps_run_seq"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("voyage_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # seq = 创建序，落库后不可变（审计与引用锚点）；清单序/执行序看 rank
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # 清单序 = 执行序：gap 编号（100/200/…），计划编辑插入取间隙值，seq 不动
    rank: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # actions.py 注册表键
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # 结构化验收 {text: 人读验收标准|None, checks: [...]|None}（docs/voyage-loop.md §6）
    acceptance: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # 闸门类型（需要人工审批的步骤）
    requires_gate: Mapped[str | None] = mapped_column(String(64))
    # 节点预算 {max_attempts?, max_tokens?, max_gpu_hours?}
    budget: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    observation: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    verdict: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)  # {passed, reason}
    # pending | running | verifying | passed | failed | obsolete
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    # 当前尝试次数；每次尝试的完整归档进 attempts（SSE 事件不持久，审计留痕一律落库）
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    # 溯源 {plan_iteration, reason?, on_failure?}：哪次计划迭代创建了它
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    tokens: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[VoyageRun] = relationship(back_populates="steps")


class VoyageTerminalLog(Base):
    """任务终端日志的持久化：结构化日志行 + 大模型完整输出，供刷新后 / 事后回看。

    只落 ``log`` 事件与 ``llm`` 完整输出（不落高频 llm_delta，实时增量仍走 SSE）；
    自增 id 即时间序，按 id 升序还原终端。尽力而为写入，绝不影响任务主流程。
    """

    __tablename__ = "voyage_terminal_logs"

    # BigInteger 在 postgres 为 BIGSERIAL/identity；sqlite 仅 INTEGER 主键才自增（回退 Integer）。
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("voyage_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event: Mapped[str] = mapped_column(String(16), nullable=False)  # 'log' | 'llm'
    level: Mapped[str | None] = mapped_column(String(16))  # log 上色 level
    stage: Mapped[str | None] = mapped_column(String(32))  # llm 环节
    message: Mapped[str] = mapped_column(Text, nullable=False)  # 日志文本 / 大模型输出全文
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
