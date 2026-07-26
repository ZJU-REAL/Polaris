"""课题「相关研究」书架（P5a）。

论文本体与解读都是纯引用（paper_id 指向全局内容池；解读读 paper_wikis，每篇一份），
个人备注挂在书架行的 note 上。

过渡期（P5a）课题 = project，topic_id 直接外键 projects；P5 引入独立 Topic 实体后
随外键平移。入架必入个人库（user_library_entries），由 services/topic_shelf.py 保证。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.paper import Paper


class TopicPaper(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """课题-论文书架行：引用内容池论文 + 入架时的 wiki 快照 + 课题语境备注。"""

    __tablename__ = "topic_papers"
    __table_args__ = (
        UniqueConstraint("topic_id", "paper_id", name="uq_topic_papers_topic_paper"),
        # 默认列表按「课题 + 未删」取行，回收站按「课题 + 已删」取行
        Index("ix_topic_papers_topic_trashed", "topic_id", "trashed_at"),
    )

    # 过渡期课题 = project（P5 拆出 Topic 实体后平移）
    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 来源方向库（溯源用；个人补充入库为空；删库置空、书架行保留靠快照兜底）
    source_library_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="SET NULL")
    )
    # 退役列（解读已统一到 paper_wikis，每篇一份、书架直接引用）：只留存量数据，
    # 代码不再读写；删列待确认稳定后另做迁移。
    wiki_snapshot: Mapped[str | None] = mapped_column(Text)
    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 课题语境的「为什么相关」备注
    note: Mapped[str | None] = mapped_column(Text)
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # 回收站：移出书架 = 软删（trashed_at 置位），可召回 / 彻底删除 / 清空。
    # 唯一键 uq_topic_papers_topic_paper 覆盖软删行，故同一篇再次入架走「复活」
    # （services/topic_shelf.py::add_to_shelf），不插新行。
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trashed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    paper: Mapped[Paper] = relationship()
