"""浏览事件：谁在什么时候打开了哪个文献库 / 哪篇论文。

单独一张表而不是复用 ``activities``：活动流是**语义事件**（每条都要有一句给人读的
message），点击是高频、无语义的数据点，混在一起会把活动流冲没。

留 ``user_id`` 只为**去重**（同一个人反复刷新不该算多次热度），API 一律不按人返回。
实验室里"谁在读什么"是敏感信息，能不暴露就不暴露。
"""

import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin

#: 目前只统计这两类。加新类型时记得同步 API 的白名单——不做白名单的话，
#: 前端写错一个字就会在表里堆出一类谁也看不懂的数据。
VIEW_KINDS = ("library", "paper")


class ViewEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "view_events"
    __table_args__ = (
        # 热度查询永远是「最近 N 天 + 按类型」，索引照这个形状建
        Index("ix_view_events_kind_created", "kind", "created_at"),
        Index("ix_view_events_target_created", "target_id", "created_at"),
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 目标 id。**不加外键**：论文和库分属两张表，而且目标删掉之后这条浏览记录
    #: 仍然是真实发生过的事，不该跟着消失（查询时按当前可见对象过滤即可）。
    target_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
