"""用户个人文献库：跨方向的浏览记录与收藏（元数据快照）。"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class UserLibraryEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """个人库条目：某用户视角下的一篇论文（跨方向按 arxiv/doi/标题去重）。

    papers 表按方向隔离且随方向级联删除，所以这里存元数据快照而非仅外键；
    last_paper_id 只是「跳回活体论文」的软引用（SET NULL）。
    saved=false 的条目是纯浏览记录；收藏即置位 saved，取消收藏不删行。
    """

    __tablename__ = "user_library_entries"
    __table_args__ = (UniqueConstraint("user_id", "dedup_key", name="uq_user_library_dedup"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 去重键：arxiv:<id> | doi:<小写doi> | title:<规范化标题sha1>（services/user_library.py 生成）
    dedup_key: Mapped[str] = mapped_column(String(512), nullable=False)
    arxiv_id: Mapped[str | None] = mapped_column(String(64))
    doi: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[list[Any] | None] = mapped_column(JSONVariant)  # [{"name": ...}]
    year: Mapped[int | None]
    venue: Mapped[str | None] = mapped_column(String(255))
    abstract: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(1024))
    tldr: Mapped[str | None] = mapped_column(Text)
    # wiki 快照：随每次浏览/收藏刷新；论文活着时前端展示实时 wiki，删除后回退到这里
    wiki_content: Mapped[str | None] = mapped_column(Text)
    saved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    visit_count: Mapped[int] = mapped_column(default=0, nullable=False)
    last_visited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_paper_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL")
    )
