"""方向文献库：实验室层的文献策展单元（P4；P7 起课题/库解耦）。

`papers` 是全局内容池（按 dedup_key 全平台唯一，只存论文本体：元数据/全文/图/embedding）；
方向对论文的「归属 + 判断」（相关性分、状态流转、库版 wiki 解读）全部落在成员表
`library_papers` 上——同一篇论文可以同时属于多个方向库，各自打分编译互不干扰，
删库只删成员行，内容池行永不删除。

P7 起课题（`Project`）与库多对多关联（`TopicSourceLibrary`）：库不再是课题的附属物，
`project_id` 语义降级为「起源课题」溯源（ondelete SET NULL——删课题不删库）；
「课题关联了哪些库」一律经 `topic_source_libraries` 查，`project_id` 仅供历史
1:1 库与管理路径（`services/libraries.py::get_library_for_project`）兜底解析。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from app.models.paper import Paper


class DirectionLibrary(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "direction_libraries"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    statement: Mapped[str | None] = mapped_column(Text)  # 方向陈述（一段话）
    rubric: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)  # 相关性评分标准
    anchors: Mapped[list[Any] | None] = mapped_column(JSONVariant)  # 锚点论文/关键词
    # 文献 ingest 状态：{"watermark": iso, "last_run": {"voyage_id", "finished_at"}}
    ingest_state: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    cadence: Mapped[str | None] = mapped_column(String(32))  # 同步节奏：daily | weekly | ...
    monthly_budget: Mapped[int | None]  # 每月 ingest 预算（P6 治理用）
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # 起源课题溯源（历史 1:1 库回指；SET NULL——删课题不再级联删库，孤儿库保留）。
    # 新建的独立库（P7 起 POST /libraries）此列恒为 NULL；「课题关联了哪些库」
    # 一律经 topic_source_libraries 查，不查这列。
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), unique=True
    )


class DirectionLibraryCurator(TimestampMixin, Base):
    """库策展人（P6 治理入口；P4 仅建表）。"""

    __tablename__ = "direction_library_curators"

    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )


class LibraryPaper(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """库-论文成员行：某方向库视角下对一篇内容池论文的归属与判断。"""

    __tablename__ = "library_papers"
    __table_args__ = (
        UniqueConstraint("library_id", "paper_id", name="uq_library_papers_library_paper"),
        Index("ix_library_papers_library_status", "library_id", "status"),
    )

    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"), index=True, nullable=False
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    relevance_score: Mapped[float | None]  # LLM 对照方向 rubric 的打分
    tldr_note: Mapped[str | None] = mapped_column(Text)  # 库视角一句话概括（可选）
    wiki_content: Mapped[str | None] = mapped_column(Text)  # 库版图文解读 markdown
    # 状态流转同 PAPER_STATUSES：candidate → scored|excluded → fetched → compiled；included 人工纳入
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    # 进垃圾桶的原因（status=excluded 时有值）：irrelevant 相关性不足自动淘汰 | manual 手动删除
    trash_reason: Mapped[str | None] = mapped_column(String(16))
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 编译 wiki_content 实际用到的模型名（取自 LLM 返回结果）
    compiled_model: Mapped[str | None] = mapped_column(String(255))

    paper: Mapped[Paper] = relationship()


class TopicSourceLibrary(Base):
    """课题 × 文献库关联（P7）：课题的语料 = 关联库论文的并集。

    多对多；课题删除级联清关联行（不动库），库删除级联清关联行（不动课题）。
    """

    __tablename__ = "topic_source_libraries"

    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
