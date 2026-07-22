"""课题「相关研究」书架（P5a）。

书架三层策略（docs-dev/workspace-ia-redesign.md §3.4）：论文本体纯引用（paper_id
指向全局内容池，永不复制）；库级 wiki 会漂移/消失 → 入架时落快照兜底，展示时
库版实时优先；个人备注挂在书架行的 note 上。

过渡期（P5a）课题 = project，topic_id 直接外键 projects；P5 引入独立 Topic 实体后
随外键平移。入架必入个人库（user_library_entries），由 services/topic_shelf.py 保证。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.paper import Paper


class TopicPaper(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """课题-论文书架行：引用内容池论文 + 入架时的 wiki 快照 + 课题语境备注。"""

    __tablename__ = "topic_papers"
    __table_args__ = (
        UniqueConstraint("topic_id", "paper_id", name="uq_topic_papers_topic_paper"),
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
    # 入架时可得的库版 wiki 快照（markdown）；展示优先级：库版实时 > 快照
    wiki_snapshot: Mapped[str | None] = mapped_column(Text)
    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 课题语境的「为什么相关」备注
    note: Mapped[str | None] = mapped_column(Text)
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    paper: Mapped[Paper] = relationship()
