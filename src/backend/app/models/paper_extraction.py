"""论文结构化抽取产物（#661，设计报告 §10 ②层）。

papers 是全平台共享的内容池，抽取产物跟着论文本体走（类比 paper_wikis /
paper_citations），因此这张表**不带 library_id**。每行一份「某 schema 对某论文」
的抽取结果：payload 按 schema 字段白名单归一化后的 JSON（services/extraction/），
同一 (paper_id, schema_id) 唯一——重跑是 UPSERT 覆盖，不留历史版本；schema 演进
靠版本号进 stage_meta，读方按需判断是否过期重抽。
"""

import uuid

from sqlalchemy import Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class PaperExtraction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一篇论文在一个抽取 schema 下的结构化产物。

    ``payload`` 只存归一化通过的字段（全空不落行）；``confidence`` 是模型对整份
    产物的自评置信度（0..1，可空——模型没给就空着，不编数）。
    """

    __tablename__ = "paper_extractions"
    __table_args__ = (
        # 每 schema 每论文一份：重抽是覆盖语义，唯一约束兜住并发重复
        UniqueConstraint("paper_id", "schema_id", name="uq_paper_extractions_paper_schema"),
    )

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 注册表里的 schema 标识（services/extraction/schemas.py），如 "skeleton"
    schema_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 归一化后的抽取结果：{字段名: str | list[str]}，键取 schema 字段白名单子集
    payload: Mapped[dict] = mapped_column(JSONVariant, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    # 溯源元信息：{"model": 实际模型名, "stage": LLM 环节, "version": schema 版本}
    stage_meta: Mapped[dict | None] = mapped_column(JSONVariant)
