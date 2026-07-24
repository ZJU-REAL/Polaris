"""实验室「每日新论文」池（Daily Paper）。

每天从 arxiv 订阅分类抓 New submissions 进池，滚动保留 7 天：
- 论文本体走全局内容池（paper_id 引用 papers，永不复制、过期不删）；
- entry 是「橱窗」行，过期由 cron 直接删除；点赞挂 entry、FK 级联跟删，
  因此「我赞过的」历史随池过期自然消失；
- 收录动作（进方向库/课题书架/个人库）直接写目标库成员表，与 entry 生命周期无关。
"""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Date, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.paper import Paper

# 默认订阅分类；可在 system_settings 的 daily_feed_categories 键覆盖
DEFAULT_DAILY_CATEGORIES = ["cs.AI", "cs.CL", "cs.CV"]

# 池滚动保留天数（含当天）
DAILY_FEED_RETENTION_DAYS = 7


class DailyFeedEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """每日池条目：一篇论文只一行（同日多分类命中合并进 categories）。"""

    __tablename__ = "daily_feed_entries"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    # 进池日期（UTC），清理与按天分组的扫描键
    feed_date: Mapped[dt.date] = mapped_column(Date, index=True, nullable=False)
    # 首个命中的订阅分类
    primary_category: Mapped[str] = mapped_column(String(32), nullable=False)
    # 命中的全部订阅分类，如 ["cs.AI", "cs.CL"]
    categories: Mapped[Any] = mapped_column(JSONVariant, nullable=False, default=list)
    # arxiv 公告类型：new（新提交）| cross（转投/交叉列表）
    announce_type: Mapped[str] = mapped_column(String(16), nullable=False, default="new")
    # 共享单篇解读（P2 编译产物）；收录时拷贝进目标库成员行
    wiki_content: Mapped[str | None] = mapped_column(Text)
    wiki_model: Mapped[str | None] = mapped_column(String(128))

    paper: Mapped[Paper] = relationship()


class DailyFeedLike(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """点赞：全实验室共享，每人每篇一赞；entry 过期删除时级联消失。"""

    __tablename__ = "daily_feed_likes"
    __table_args__ = (UniqueConstraint("entry_id", "user_id", name="uq_daily_feed_likes"),)

    entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("daily_feed_entries.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
