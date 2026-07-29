"""文献库每日简报与滚动趋势快照。"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import Date, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class LibraryResearchDigest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次文献同步形成的、可长期回看的研究简报。

    ``content`` 是面向人的 Markdown；结构化 JSON 字段供前端统计卡片、论文跳转和
    后续趋势综合使用。Obsidian 历史简报与 Polaris 原生 Voyage 简报共用这张表。
    """

    __tablename__ = "library_research_digests"
    __table_args__ = (
        UniqueConstraint(
            "library_id",
            "report_date",
            "source",
            name="uq_library_research_digests_library_date_source",
        ),
        UniqueConstraint("voyage_id", name="uq_library_research_digests_voyage"),
        Index("ix_library_research_digests_library_date", "library_id", "report_date"),
    )

    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"), nullable=False
    )
    # Obsidian 历史导入没有 Voyage；原生流水线必须有，且一次 Voyage 至多一份简报。
    voyage_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voyage_runs.id", ondelete="SET NULL"), nullable=True
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # voyage | obsidian
    mode: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # bootstrap | incremental | import

    counts: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    source_diagnostics: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    paper_insights: Mapped[list[Any]] = mapped_column(JSONVariant, nullable=False)
    excluded_papers: Mapped[list[Any]] = mapped_column(JSONVariant, nullable=False)
    cross_paper_signals: Mapped[list[Any]] = mapped_column(JSONVariant, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))

    # 趋势既保留结构化线程，也保留可直接阅读/导出的 Markdown 快照。
    rolling_trends: Mapped[list[Any]] = mapped_column(JSONVariant, nullable=False)
    trend_content: Mapped[str] = mapped_column(Text, nullable=False)
    trend_model: Mapped[str | None] = mapped_column(String(128))

    @property
    def has_trends(self) -> bool:
        return bool(self.trend_content or self.rolling_trends)
