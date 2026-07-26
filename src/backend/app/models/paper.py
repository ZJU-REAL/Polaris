"""论文内容池（全局）、解读、概念（wiki 词条）、笔记 / 标签 / 个人阅读状态与其关联表。

P4 起 ``papers`` 是全平台共享的内容池（按 dedup_key 唯一，只存论文本体）；
方向对论文的归属与判断字段（相关性分 / 状态）在 ``library_papers``
（models/library_direction.py）。解读不分方向——每篇论文一份，见 :class:`PaperWiki`。"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.orm.attributes import set_committed_value

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

EMBEDDING_DIM = 1024  # BGE-M3（lab LiteLLM /v1/embeddings）

# postgres 用 pgvector（语义检索），sqlite 等回退 JSON 存 list（仅存不查）
EmbeddingVariant = JSON().with_variant(Vector(EMBEDDING_DIM), "postgresql")

# 论文在方向库内的状态流转（LibraryPaper.status）：candidate →(打分) scored | excluded
# →(下载全文) fetched →(Librarian 编译) compiled；included/excluded 亦可人工覆盖
PAPER_STATUSES = ("candidate", "scored", "excluded", "fetched", "compiled", "included")

# 概念的把关状态（Concept.status，见 services/concepts.py）：
# candidate = 刚从解读里抽出来、还只有 1 篇论文用到，不对用户可见、也不花钱写定义；
# active    = 被 ≥2 篇论文用到、且模型判定确实是个学术概念，进概念库；
# rejected  = 模型判定它根本不是概念（图表引用 fig:1、编号 12、半句话……）。
# candidate 与 rejected 的区别：前者是「还没够格」，引用涨上来会转正；后者是终态，
# 不再复判——判过一次就别再为它花钱。
CONCEPT_STATUS_CANDIDATE = "candidate"
CONCEPT_STATUS_ACTIVE = "active"
CONCEPT_STATUS_REJECTED = "rejected"
CONCEPT_STATUSES = (
    CONCEPT_STATUS_CANDIDATE,
    CONCEPT_STATUS_ACTIVE,
    CONCEPT_STATUS_REJECTED,
)

paper_concepts = Table(
    "paper_concepts",
    Base.metadata,
    Column("paper_id", ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("concept_id", ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True),
)


class Paper(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "papers"

    # 全局去重键：arxiv:<id> | doi:<小写doi> | title:<标题+年份+首作者sha1>（services/dedup.py）
    dedup_key: Mapped[str | None] = mapped_column(String(512), unique=True, index=True)
    source: Mapped[str | None] = mapped_column(String(32))  # arxiv | semantic_scholar | manual
    arxiv_id: Mapped[str | None] = mapped_column(String(64), index=True)
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    external_ids: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)  # {arxiv, s2, doi..}
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[list[Any] | None] = mapped_column(JSONVariant)  # [{"name": ...}]
    affiliations: Mapped[list[Any] | None] = mapped_column(JSONVariant)  # 发表机构 ["MIT", ...]
    abstract: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None]
    venue: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1024))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_path: Mapped[str | None] = mapped_column(String(1024))
    full_text_path: Mapped[str | None] = mapped_column(String(1024))
    tldr: Mapped[str | None] = mapped_column(Text)
    # 提取的论文图列表：[{index, page, width, height, caption: str|null, important: bool}]，
    # 图片文件落 <data_dir>/papers/<paper_id>/figures/fig_<index>.png（路径不出 API）
    figures: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVariant)
    # 论文级向量的构建元信息（前端索引状态悬浮显示）；存量数据 / 未建为 null
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedding_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 分块向量最近一次补齐的元信息（分段本身在 paper_chunks，元信息汇总记这里，
    # 避免在上百万分段行上各存一份时间/模型名）
    chunk_embedding_model: Mapped[str | None] = mapped_column(String(128))
    chunk_embedding_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 这篇论文的概念（论文详情 / 导出 / agent 工具的展示口径）：**只给转正概念**。
    # 候选词条（还只有这一篇论文用到，多半是这篇自己起的 benchmark / 模型代号）不对
    # 用户可见，过滤放在关系本身上，所有读路径一处收口。关联的增删一律直接写
    # paper_concepts（services/concepts.py），没有任何地方经这个关系写，故 viewonly。
    concepts: Mapped[list["Concept"]] = relationship(
        secondary=paper_concepts,
        primaryjoin=lambda: Paper.id == paper_concepts.c.paper_id,
        secondaryjoin=lambda: (Concept.id == paper_concepts.c.concept_id)
        & (Concept.status == CONCEPT_STATUS_ACTIVE),
        viewonly=True,
    )
    # 唯一解读（PaperWiki）；selectin 随论文一起取，读路径无需显式 join
    wiki: Mapped["PaperWiki | None"] = relationship(
        back_populates="paper", uselist=False, lazy="selectin", cascade="all, delete-orphan"
    )

    @property
    def pdf_available(self) -> bool:
        return bool(self.pdf_path)

    @property
    def wiki_content(self) -> str | None:
        """解读正文（没有解读为 None）——全平台唯一一份，见 :class:`PaperWiki`。"""
        return self.wiki.content if self.wiki is not None else None


class PaperWiki(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """论文解读：每篇论文一份，全平台共享。

    早期按方向库分版本存在 library_papers.wiki_content，但同一篇论文在不同库
    里的解读实际都是通用解读，分版本只带来重复编译与「同一篇看到不同内容」。
    编译输入不带任何库的方向陈述/rubric，产出即通用解读；谁都能重新编译，
    以最新一次为准（覆盖本行，compiled_by 一并更新）。
    """

    __tablename__ = "paper_wikis"
    __table_args__ = (UniqueConstraint("paper_id", name="uq_paper_wikis_paper"),)

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)  # 图文解读 markdown
    model: Mapped[str | None] = mapped_column(String(128))  # 编译实际所用模型名
    # 最后一次编译的人（存量迁移数据无从得知，留空）
    compiled_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    paper: Mapped[Paper] = relationship(back_populates="wiki")


def new_paper(**fields: Any) -> Paper:
    """建内容池论文行（写路径统一入口）。

    顺带把 ``wiki`` 标成「已加载且为空」——新论文当然还没有解读；不标的话，
    落库后第一次读 ``paper.wiki_content`` 会触发一次隐式懒加载，异步 session 下
    直接抛 MissingGreenlet。
    """
    paper = Paper(**fields)
    set_committed_value(paper, "wiki", None)
    return paper


# 分段来源：fulltext=从 PDF 全文切分 | abstract=无全文时用标题+摘要兜底的单块
CHUNK_SOURCES = ("fulltext", "abstract")


class PaperChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """论文分段（文献问答 / idea 生成的检索底座）。

    有全文时按全文确定性切分（services/chunks.py）；没有全文的论文退化为一个
    「标题 + 摘要」块，保证每篇论文对检索都存在（否则每日推送这类不下 PDF 的
    论文对文献对话完全不可见）。embedding 由 wiki.link_concepts 步骤批量补齐
    （provider 不支持时留空，检索降级关键词）。

    ``source`` 区分两类块：拿到 PDF 后重建分段会把摘要块整体替换为全文块，
    「这篇有没有全文索引」也靠它判断（不能只看有没有分段行）。
    """

    __tablename__ = "paper_chunks"
    __table_args__ = (UniqueConstraint("paper_id", "seq", name="uq_paper_chunks_paper_seq"),)

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # 存量分段都来自全文，故 server_default="fulltext"
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="fulltext", server_default="fulltext"
    )
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVariant)


READING_STATUSES = ("unread", "reading", "read")

paper_tag_links = Table(
    "paper_tag_links",
    Base.metadata,
    Column("paper_id", ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("paper_tags.id", ondelete="CASCADE"), primary_key=True),
)


class PaperNote(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """论文笔记（paper × author）：跨课题共享，仅作者本人（或平台 admin）可见可改删。"""

    __tablename__ = "paper_notes"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)


HIGHLIGHT_COLORS = ("yellow", "green", "blue", "pink", "purple")
# 标注样式：整段高亮 / 下方横线 / 下方波浪线
HIGHLIGHT_STYLES = ("highlight", "underline", "wave")


class PaperHighlight(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """PDF 划线标注：文本层里选中的句子 + 可选批注。

    定位策略「文本锚点为主 + 归一化坐标加速」：selected_text 存选中的原文
    （PDF 换版重抽也能靠文本回锚），rects 存归一化到页面 0..1 的矩形列表
    （每行一个，渲染时按当前页宽高还原色块，缩放无关）。
    归属同 PaperNote（paper × author）：跨课题共享，仅作者本人（或平台 admin）可见可改删。
    """

    __tablename__ = "paper_highlights"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    page: Mapped[int] = mapped_column(nullable=False)  # 1-indexed 页码
    # 归一化矩形列表 [{"x0","y0","x1","y1"}]，值域 0..1（相对页面左上角）
    rects: Mapped[list[Any]] = mapped_column(JSONVariant, nullable=False)
    selected_text: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str] = mapped_column(String(16), default="yellow", nullable=False)
    # 标注样式：highlight（高亮块）| underline（下方横线）| wave（下方波浪线）
    style: Mapped[str] = mapped_column(String(16), default="highlight", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)  # 挂在划线上的批注，可空


class PaperTag(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """库级论文标签（同库内名字唯一）；与论文多对多（paper_tag_links）。

    P9e：标签作用域从课题（project_id）改为文献库（library_id）——独立库也能打标签。
    课题的标签操作解析到其隐式起源库后走库级实现。"""

    __tablename__ = "paper_tags"
    __table_args__ = (UniqueConstraint("library_id", "name", name="uq_paper_tags_library_name"),)

    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class PaperUserMeta(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """论文的个人视角状态：星标 + 阅读状态（每人每篇至多一条）。"""

    __tablename__ = "paper_user_meta"
    __table_args__ = (UniqueConstraint("paper_id", "user_id", name="uq_paper_user_meta"),)

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    starred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reading_status: Mapped[str] = mapped_column(
        String(16), default="unread", nullable=False
    )  # unread | reading | read


class UserPaperTag(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """个人标签（paper × user × name）：任何登录用户都能给自己可读的论文打，
    只有本人可见可改。与库作用域的 PaperTag 完全独立——库标签是共享资产（整组
    覆盖会盖掉别人打的），个人标签是私域。

    扁平设计：名字内联在这张表上，不另建标签实体。个人标签没有跨用户共享语义，
    不需要「改一处到处生效」，也就不需要库标签那套零引用回收
    （对照 papers.prune_orphan_tags）。
    """

    __tablename__ = "user_paper_tags"
    __table_args__ = (
        UniqueConstraint("user_id", "paper_id", "name", name="uq_user_paper_tags"),
        Index("ix_user_paper_tags_user_name", "user_id", "name"),  # 「我的所有标签」列表 / 按标签筛
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Concept(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """概念词条：全平台一份，slug 全局唯一。

    早期按方向库分版本（concepts.library_id），同一个词在每个库各存一份、定义还各不
    相同；解读统一成论文级之后更没有归属可言（每日推送的论文不属于任何库）。
    「某个库有哪些概念」不再存，一律推导：库的论文（library_papers）→ 关联概念
    （paper_concepts）→ 去重，见 services/concepts.py::library_concept_ids。

    ``status`` 是入库把关：解读里标出来的词先记 candidate，被第 2 篇论文用到才复核转正
    成 active。只标一次的绝大多数是这篇论文自己起的名字（benchmark / 数据集 / 模型代号），
    留在候选里既不进概念库也不花钱写定义；转正复核时模型判定「根本不是概念」的
    （fig:1、编号、半句话）落 rejected 终态。
    """

    __tablename__ = "concepts"
    __table_args__ = (UniqueConstraint("slug", name="uq_concepts_slug"),)

    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    definition: Mapped[str | None] = mapped_column(Text)  # LLM 一句话定义（转正后才生成）
    # method | architecture | methodology | problem | metric | dataset | other
    category: Mapped[str | None] = mapped_column(String(64))
    wiki_content: Mapped[str | None] = mapped_column(Text)  # markdown
    # candidate（默认，不可见）| active（转正，进概念库）| rejected（不是概念，终态）
    status: Mapped[str] = mapped_column(
        String(16),
        default=CONCEPT_STATUS_CANDIDATE,
        server_default=CONCEPT_STATUS_CANDIDATE,
        nullable=False,
    )
    # 最近一次由模型判定「确实是个学术概念」的时间。为空 = 还没判过：门槛改造之前
    # 存量转正的那批就是空的，下一次概念同步会连同定义一起复核一遍（判完不再复判）。
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 概念的关联论文（概念详情 / 导出）：这里不按状态过滤——能拿到这个概念说明它已经
    # 可见了，它的论文清单就是全部关联。写关联同样不经这个关系，故 viewonly。
    papers: Mapped[list[Paper]] = relationship(secondary=paper_concepts, viewonly=True)
