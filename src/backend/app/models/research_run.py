"""研究：用户输入一个方向，引擎依次检索文献、寻找假设、把留下来的写成想法。

一行 = 一个方向 = 一个课题 + 它自己的文献库。每个阶段是一条独立的任务（文献检索
用 ``wiki_bootstrap``，寻找假设用 ``discovery``），这一行只记「现在到哪个阶段、
这个阶段的任务是哪一条」。阶段之间怎么推进见 services/research_runs.py。
"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

# 阶段：确定范围 → 检索文献 → 寻找假设 → 等用户选定想法
RESEARCH_PHASES = ("plan", "survey", "hypotheses", "choose")

# 状态。running：引擎在干活（或在等自动重试）；waiting_user：等用户回答问题或选定想法；
# needs_attention：这个阶段没走通，``attention`` 里说明原因，用户点一下重试或改范围；
# done：用户已经选定了想法；cancelled：用户停止了它。
RESEARCH_STATUSES = ("running", "waiting_user", "needs_attention", "done", "cancelled")


class ResearchRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "research_runs"

    # 一个方向一个课题（唯一）：删课题连这一行一起删
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    # 这个方向自己的文献库；确定范围完成之前为空
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="SET NULL")
    )
    direction: Mapped[str] = mapped_column(Text, nullable=False)  # 用户输入的原文
    # 检索简报（schemas/research.ResearchBrief）；确定范围完成之前为空
    brief: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    phase: Mapped[str] = mapped_column(String(16), default="plan", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="running", index=True, nullable=False)
    # needs_attention 时的原因：{code, detail, counts}
    attention: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # {plan: {attempts, last_error, overrides}, survey: {voyage_id, attempts, history[]},
    #  hypotheses: {voyage_id, attempts, history[]}}
    phases: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # 前端生成的幂等键：同一个键重复提交只建一次
    client_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
