"""我发表的论文：作者身份绑定 + 发表候选/确认记录（用户级，方向无关）。"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

PUBLICATION_STATUSES = ("pending", "confirmed", "rejected")
PUBLICATION_SOURCES = ("openalex", "manual", "library")


class UserAuthorProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """作者身份绑定（每用户一条）：姓名变体 + 机构 + 外部作者实体 id。

    优先绑 OpenAlex 作者实体（已消歧）；搜不到实体时退化为纯姓名+机构字符串。
    """

    __tablename__ = "user_author_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    name_variants: Mapped[list[Any]] = mapped_column(JSONVariant, nullable=False)  # ["Y. Shen"..]
    affiliations: Mapped[list[Any]] = mapped_column(JSONVariant, nullable=False)  # 现任+历史机构
    openalex_author_id: Mapped[str | None] = mapped_column(String(64))
    s2_author_id: Mapped[str | None] = mapped_column(String(64))
    orcid: Mapped[str | None] = mapped_column(String(32))
    auto_sync: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserPublication(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """发表记录候选：外部同步/手动补录产生，经用户确认（pending→confirmed|rejected）。

    rejected 保留不删——阻止下次同步把同一篇再次推成候选。
    """

    __tablename__ = "user_publications"
    __table_args__ = (UniqueConstraint("user_id", "dedup_key", name="uq_user_publications_dedup"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 去重键：doi:<小写doi> | arxiv:<id> | title:<规范化标题sha1>（services/publications.py 生成）
    dedup_key: Mapped[str] = mapped_column(String(512), nullable=False)
    openalex_id: Mapped[str | None] = mapped_column(String(64))
    arxiv_id: Mapped[str | None] = mapped_column(String(64))
    doi: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[list[Any] | None] = mapped_column(JSONVariant)  # [{"name": ...}]
    year: Mapped[int | None]
    venue: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1024))
    cited_by_count: Mapped[int | None]
    # 库内匹配来源的活体论文软引用（跳转阅读页用）；论文/方向删除后置空
    paper_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("papers.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # openalex | manual | library
    status: Mapped[str] = mapped_column(  # pending | confirmed | rejected
        String(16), default="pending", nullable=False, index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
