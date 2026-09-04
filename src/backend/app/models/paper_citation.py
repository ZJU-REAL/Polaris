"""论文引文边（#639：引文意图分类 + OpenAlex 对齐的存储落点）。

papers 是全平台共享的内容池，引文关系跟着论文本体走（类比 paper_wikis /
paper_chunks），因此这张表**不带 library_id**——「某个库的引文网络」一律由
库成员（library_papers）推导，不另存一份归属。

每行一条「citing → 参考文献条目」边：cited_ref_raw 永远保留解析出的条目原文
（被引论文不在池内时它就是全部信息）；cited_paper_id 是尽力而为的池内对齐，
被引论文后来入库/被删都不影响边本身（SET NULL）。
"""

import uuid

from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin

# 引文意图五档（设计报告 §10 ③层，#639）：
# background 背景铺垫 | method 方法沿用 | comparison 实验对比 |
# support 结论支持 | contrast 观点相左
CITATION_INTENTS = ("background", "method", "comparison", "support", "contrast")


class PaperCitation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """引文边：citing 论文的第 ref_index 条参考文献。

    ``intent`` / ``confidence`` 由 citation_intent 环节的 LLM 分类回填，
    分类前为 NULL（前端按「未分类」分组展示）。"""

    __tablename__ = "paper_citations"
    __table_args__ = (
        # 同一篇论文的参考文献序号唯一：重建引文边先删后插，靠它兜住并发重复
        UniqueConstraint("citing_paper_id", "ref_index", name="uq_paper_citations_citing_ref"),
    )

    citing_paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 池内对齐（可空）：被引论文恰好也在内容池时指过去；删除被引论文只断链不删边
    cited_paper_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL"), index=True
    )
    # 参考文献序号（1 起；无编号的参考文献列表按出现顺序编）
    ref_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 参考文献条目原文（解析产物，永远保留——池外文献只有这一份信息）
    cited_ref_raw: Mapped[str] = mapped_column(Text, nullable=False)
    # 正文中的引用上下文句（[n] 标记所在句）；作者-年份式引用解析不到时为 NULL，
    # 意图分类降级用「条目原文 + citing 论文标题摘要」
    context: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(16))  # CITATION_INTENTS 之一
    confidence: Mapped[float | None] = mapped_column(Float)
