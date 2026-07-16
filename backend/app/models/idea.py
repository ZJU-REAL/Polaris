"""研究想法（ideation 产物），带四维评分与 Elo 排位。"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.paper import EmbeddingVariant

# 状态流转：candidate →(锦标赛) under_review →(闸门批准) promoted；人工可置 rejected
IDEA_STATUSES = ("candidate", "under_review", "promoted", "rejected")

# 深度分级（docs/api-idea2.md §7）：sketch=阶段0方向草案，proposal=深耕产物（Research Proposal）
IDEA_DEPTHS = ("sketch", "proposal")

# goal.research_type 枚举（docs/api-idea2.md §3）
RESEARCH_TYPES = ("method", "benchmark", "analysis", "survey", "application", "theory")


class Idea(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ideas"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)  # markdown：动机/方法概述/预期实验/风险
    # {"novelty": .., "feasibility": .., "operability": .., "impact": ..}（0-10）
    scores: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # {"novelty": "理由", ...} 与 scores 同维度
    score_rationale: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    elo_rating: Mapped[float] = mapped_column(default=1200.0, nullable=False)
    matches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 锦标赛对局数
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # candidate | under_review | promoted | rejected
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    parent_paper_ids: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    # 语义去重用：postgres pgvector(1024)，sqlite 回退 JSON（同 papers.embedding）
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVariant)
    # ---- Idea 2.0（docs/api-idea2.md §7） ----
    # sketch | proposal
    depth: Mapped[str] = mapped_column(String(16), default="sketch", nullable=False)
    # method | benchmark | analysis | survey | application | theory（深耕产物才有）
    research_type: Mapped[str | None] = mapped_column(String(32))
    # 结构化研究目标（goal schema，docs/api-idea2.md §3）
    goal: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # 依据文献：[{paper_id|null, title, url|null, why, source: library|external|signal}]
    evidence: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    # 深化来源（seed.type=idea 时指向被深化的草案）
    seed_idea_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ideas.id", ondelete="SET NULL")
    )
