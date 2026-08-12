"""论文库与检索业务逻辑（不 import fastapi）。

P4 起 ``papers`` 是全局内容池：方向维度的归属/判断（status、相关性分）在
``library_papers`` 成员行上，解读则是论文级唯一一份（``paper_wikis``）。API 形状不变
（仍收 project_id），这里解析到隐式库后以 (Paper, LibraryPaper) 联查，并用
:class:`PaperView` 还原旧单表字段口径给 schema。
"""

import asyncio
import json
import logging
import shutil
import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Select, and_, cast, delete, exists, false, func, insert, or_, select, text
from sqlalchemy import Text as SAText
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.embedding_space import EmbeddingSpace
from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.daily_feed import DailyFeedEntry
from app.models.library import UserLibraryEntry
from app.models.library_direction import LibraryPaper
from app.models.paper import (
    Concept,
    Paper,
    PaperNote,
    PaperTag,
    PaperUserMeta,
    PaperWiki,
    UserPaperTag,
    paper_tag_links,
)
from app.models.project import ProjectMember
from app.models.publication import UserPublication
from app.models.topic_shelf import TopicPaper
from app.services import concepts as concepts_service
from app.services.libraries import (
    dedupe_member_rows,
    get_library_for_project,
    get_source_library_ids,
    member_paper_stmt,
    member_papers_stmt,
    user_visible_paper_stmt,
)

logger = logging.getLogger(__name__)

PAPER_SORTS = (
    "relevance",
    "-published_at",
    "published_at",
    "-created_at",
    "created_at",
)

# 语义检索重排：向量召回候选数 / 送重排的文档截断长度
RERANK_CANDIDATES = 30
RERANK_DOC_CHARS = 512

# status 组别名：可见（检索到的全部，不含回收站）/ 库内（相关性达标及之后）/
# 待编译（达标但未编译）/ 已编译（含人工纳入的历史数据）
PAPER_STATUS_GROUPS: dict[str, tuple[str, ...]] = {
    "visible": ("candidate", "scored", "fetched", "compiled", "included"),
    "library": ("scored", "fetched", "compiled", "included"),
    "pending_compile": ("scored", "fetched"),
    "compiled_any": ("compiled", "included"),
}

# AI 伴读上下文：full_text 截断上限（超长时头尾各留一半）
CHAT_CONTEXT_MAX_CHARS = 80_000


class PdfSourceUnsupportedError(Exception):
    """论文无 arxiv_id，不支持自动补下 PDF。"""


class PdfFetchFailedError(Exception):
    """PDF 下载失败（上游不可达 / 非 200 等）。"""


class PdfAlreadyExistsError(Exception):
    """论文已经有可用 PDF；上传接口不允许静默覆盖原件。"""


class PdfUploadInvalidError(Exception):
    """上传内容不是可读取的 PDF。"""


class PaperView:
    """内容池论文 + 库成员行的合并视角（字段口径与旧单表 Paper 一致）。

    本体字段（id/title/authors/pdf_path/…）与解读（wiki_content/compiled_*）透传
    Paper——解读全平台一份，不随方向变；方向维度的判断字段（status/relevance_score/…）
    取成员行；``project_id`` 为本次访问解析出的方向（过渡期 = 成员行所属隐式库回指的
    project）。``created_at`` 口径 = 加入本方向库的时间（成员行 created_at）。
    """

    __slots__ = ("paper", "membership", "project_id")

    def __init__(
        self, paper: Paper, membership: LibraryPaper, project_id: uuid.UUID | None
    ) -> None:
        self.paper = paper
        self.membership = membership
        self.project_id = project_id

    def __getattr__(self, name: str) -> Any:  # 本体字段透传
        return getattr(object.__getattribute__(self, "paper"), name)

    @property
    def id(self) -> uuid.UUID:
        return self.paper.id

    @property
    def library_id(self) -> uuid.UUID | None:
        """成员行所属库；池级兜底视角（合成的临时成员行）无库 → None。"""
        return self.membership.library_id

    @property
    def relevance_score(self) -> float | None:
        return self.membership.relevance_score

    @property
    def status(self) -> str:
        return self.membership.status

    @property
    def trash_reason(self) -> str | None:
        return self.membership.trash_reason

    @property
    def scored_at(self) -> datetime | None:
        return self.membership.scored_at

    @property
    def compiled_at(self) -> datetime | None:
        """最后一次编译解读的时间（解读行的 updated_at）。"""
        return self.paper.wiki.updated_at if self.paper.wiki is not None else None

    @property
    def compiled_model(self) -> str | None:
        return self.paper.wiki.model if self.paper.wiki is not None else None

    @property
    def wiki_content(self) -> str | None:
        return self.paper.wiki_content

    @property
    def has_wiki(self) -> bool:
        return self.paper.wiki is not None

    @property
    def created_at(self) -> datetime:
        return self.membership.created_at


async def _read_library_ids(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    library_id: uuid.UUID | None,
    library_ids: Sequence[uuid.UUID] | None = None,
) -> list[uuid.UUID]:
    """并集读路径的库解析：显式 library_ids（全局助手已按可见性算好的一组库）→ 原样用；
    显式 library_id（单库读视图/库工作台）→ [library_id]；
    否则按课题关联库并集（P7；空关联=空语料，调用方返回空态而非报错）。"""
    if library_ids is not None:
        return list(library_ids)
    if library_id is not None:
        return [library_id]
    assert project_id is not None
    return await get_source_library_ids(session, project_id)


def apply_paper_filters(
    stmt: Select,
    *,
    library_ids: Sequence[uuid.UUID] | None = None,
    status: str | None = None,
    q: str | None = None,
    tag: str | None = None,
    my_tag: str | None = None,
    starred: bool | None = None,
    reading_status: str | None = None,
    user_id: uuid.UUID | None = None,
    author: str | None = None,
    affiliation: str | None = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    daily_only: bool = False,
) -> Select:
    """论文列表 / 引用导出共用的过滤条件（作用于已 join 成员表的语句）。

    调用方须以 :func:`app.services.libraries.member_paper_stmt` 为基础语句
    （本函数只加 WHERE，不负责 join）。status 支持组别名（docs/api-lit.md §8.5）。
    """
    if status in PAPER_STATUS_GROUPS:
        stmt = stmt.where(LibraryPaper.status.in_(PAPER_STATUS_GROUPS[status]))
    elif status:
        stmt = stmt.where(LibraryPaper.status == status)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(Paper.title.ilike(pattern), Paper.abstract.ilike(pattern)))
    # 高级检索（docs/api-lit.md §8.7）：作者/机构在 JSON 列上做文本包含匹配（两种方言通用）
    if author:
        stmt = stmt.where(cast(Paper.authors, SAText).ilike(f"%{author}%"))
    if affiliation:
        stmt = stmt.where(cast(Paper.affiliations, SAText).ilike(f"%{affiliation}%"))
    if published_from:
        stmt = stmt.where(
            or_(
                Paper.published_at >= published_from,
                and_(Paper.published_at.is_(None), Paper.year >= published_from.year),
            )
        )
    if published_to:
        stmt = stmt.where(
            or_(
                Paper.published_at <= published_to,
                and_(Paper.published_at.is_(None), Paper.year <= published_to.year),
            )
        )
    if created_from:
        stmt = stmt.where(LibraryPaper.created_at >= created_from)
    if created_to:
        stmt = stmt.where(LibraryPaper.created_at <= created_to)
    if daily_only:
        # 「今日新收录」只认从每日论文池自动进来的：手动加的、引文扩展来的不算
        stmt = stmt.where(Paper.id.in_(select(DailyFeedEntry.paper_id)))
    if tag and library_ids:
        stmt = stmt.where(
            Paper.id.in_(
                select(paper_tag_links.c.paper_id)
                .join(PaperTag, PaperTag.id == paper_tag_links.c.tag_id)
                .where(PaperTag.library_id.in_(library_ids), PaperTag.name == tag)
            )
        )
    if my_tag:
        stmt = stmt.where(
            exists().where(
                UserPaperTag.paper_id == Paper.id,
                UserPaperTag.user_id == user_id,
                UserPaperTag.name == my_tag,
            )
        )
    if starred is not None:
        starred_exists = exists().where(
            PaperUserMeta.paper_id == Paper.id,
            PaperUserMeta.user_id == user_id,
            PaperUserMeta.starred.is_(True),
        )
        stmt = stmt.where(starred_exists if starred else ~starred_exists)
    if reading_status:
        status_sub = (
            select(PaperUserMeta.reading_status)
            .where(PaperUserMeta.paper_id == Paper.id, PaperUserMeta.user_id == user_id)
            .scalar_subquery()
        )
        stmt = stmt.where(func.coalesce(status_sub, "unread") == reading_status)
    return stmt


async def list_papers(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    library_ids: Sequence[uuid.UUID] | None = None,
    status: str | None = None,
    q: str | None = None,
    tag: str | None = None,
    my_tag: str | None = None,
    starred: bool | None = None,
    reading_status: str | None = None,
    user_id: uuid.UUID | None = None,
    sort: str = "relevance",
    page: int = 1,
    size: int = 20,
    author: str | None = None,
    affiliation: str | None = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    daily_only: bool = False,
    last_sync_only: bool = False,
) -> tuple[Sequence[PaperView], int]:
    """库内论文列表。入口可为 library_id（单库读视图/库工作台）、project_id
    （课题成员视角 = 关联库并集，P7），或调用方已经完成权限计算的 library_ids。
    project_id 兼作 PaperView 的课题上下文回填。

    单库（含课题只关联一个库的常见情形）走 SQL 分页快路径；课题关联多库时跨库
    同一论文按确定性视角归并（相关性分高者优先），Python 侧排序 + 分页保证可移植。"""
    library_ids = await _read_library_ids(
        session,
        project_id=project_id,
        library_id=library_id,
        library_ids=library_ids,
    )
    if not library_ids:
        return [], 0

    # 「最近一次同步新增」：按各库自己最后一轮 ingest 的开始时刻卡。
    # 不能用「今天入库」——上次同步要是在昨天，今天就永远是 0 篇，而用户想看的是
    # 「上次更新带进来什么」。没跑过同步的库返回空集（0 篇），而不是退化成全部。
    last_sync_clause = None
    if last_sync_only:
        from app.models.voyage import VoyageRun

        rows = (
            await session.execute(
                select(VoyageRun.library_id, func.max(VoyageRun.created_at))
                .where(VoyageRun.kind == "wiki_ingest", VoyageRun.library_id.in_(library_ids))
                .group_by(VoyageRun.library_id)
            )
        ).all()
        per_library = [
            and_(LibraryPaper.library_id == lib_id, LibraryPaper.created_at >= started)
            for lib_id, started in rows
            if started is not None
        ]
        # 一次都没同步过 → 没有「上次新增」可言，给一个恒假条件而不是放行
        last_sync_clause = or_(*per_library) if per_library else false()

    filter_kwargs = dict(
        library_ids=library_ids,
        status=status,
        q=q,
        tag=tag,
        my_tag=my_tag,
        starred=starred,
        reading_status=reading_status,
        user_id=user_id,
        author=author,
        affiliation=affiliation,
        published_from=published_from,
        published_to=published_to,
        created_from=created_from,
        created_to=created_to,
        daily_only=daily_only,
    )

    if len(library_ids) == 1:
        stmt = apply_paper_filters(member_paper_stmt(library_ids[0]), **filter_kwargs)
        if last_sync_clause is not None:
            stmt = stmt.where(last_sync_clause)
        total = (await session.execute(stmt.with_only_columns(func.count()))).scalar_one()
        if sort == "-published_at":
            stmt = stmt.order_by(
                Paper.published_at.desc().nulls_last(),
                LibraryPaper.created_at.desc(),
                Paper.id.asc(),
            )
        elif sort == "published_at":
            stmt = stmt.order_by(
                Paper.published_at.asc().nulls_last(),
                LibraryPaper.created_at.asc(),
                Paper.id.asc(),
            )
        elif sort == "-created_at":
            stmt = stmt.order_by(LibraryPaper.created_at.desc(), Paper.id.asc())
        elif sort == "created_at":
            stmt = stmt.order_by(LibraryPaper.created_at.asc(), Paper.id.asc())
        else:  # relevance（默认）
            stmt = stmt.order_by(
                LibraryPaper.relevance_score.desc().nulls_last(),
                LibraryPaper.created_at.desc(),
                Paper.id.asc(),
            )
        stmt = stmt.offset((page - 1) * size).limit(size)
        rows = (await session.execute(stmt)).all()
        return [PaperView(paper, membership, project_id) for paper, membership in rows], int(total)

    # 关联多库并集：过滤 → 跨库归并 → Python 排序 + 分页
    stmt = apply_paper_filters(member_papers_stmt(library_ids), **filter_kwargs)
    if last_sync_clause is not None:
        stmt = stmt.where(last_sync_clause)
    all_rows = dedupe_member_rows((await session.execute(stmt)).all())
    if sort == "-published_at":
        all_rows.sort(
            key=lambda pm: (
                pm[0].published_at is None,
                -(pm[0].published_at.timestamp() if pm[0].published_at else 0.0),
                -pm[1].created_at.timestamp(),
                str(pm[0].id),
            )
        )
    elif sort == "published_at":
        all_rows.sort(
            key=lambda pm: (
                pm[0].published_at is None,
                pm[0].published_at.timestamp() if pm[0].published_at else 0.0,
                pm[1].created_at.timestamp(),
                str(pm[0].id),
            )
        )
    elif sort == "-created_at":
        all_rows.sort(key=lambda pm: (-pm[1].created_at.timestamp(), str(pm[0].id)))
    elif sort == "created_at":
        all_rows.sort(key=lambda pm: (pm[1].created_at.timestamp(), str(pm[0].id)))
    else:  # relevance（默认）
        all_rows.sort(
            key=lambda pm: (
                -(pm[1].relevance_score if pm[1].relevance_score is not None else -1e18),
                -pm[1].created_at.timestamp(),
                str(pm[0].id),
            )
        )
    total = len(all_rows)
    start = (page - 1) * size
    page_rows = all_rows[start : start + size]
    return [PaperView(paper, membership, project_id) for paper, membership in page_rows], int(total)


async def _paper_in_daily_feed(session: AsyncSession, paper_id: uuid.UUID) -> bool:
    """论文是否在当前每日推送里——每日池全实验室可读，登录用户即可读它。"""
    from app.models.daily_feed import DailyFeedEntry

    row = (
        await session.execute(
            select(DailyFeedEntry.id).where(DailyFeedEntry.paper_id == paper_id).limit(1)
        )
    ).first()
    return row is not None


async def _pool_paper_view(
    session: AsyncSession, *, paper_id: uuid.UUID, user_id: uuid.UUID, with_concepts: bool
) -> PaperView | None:
    """池级可见性兜底（P5b）：论文不在任何可见方向库，但个人链路可达时仍可读。

    可达条件：该论文在请求者任一课题的相关研究书架上、在其个人库条目里
    （dedup 匹配），或仍在每日推送池里（每日推送全实验室可读，未收录也能读）。
    返回的视角带**临时成员行**（不入 session、永不落库）：
    status=included、无判断字段；``project_id`` 取最早入架的课题（仅个人库
    可达时为 None）。只用于读路径——写成员行的端点不开启池级兜底。
    """
    from app.models.topic_shelf import TopicPaper
    from app.services import user_library

    options = (selectinload(Paper.concepts),) if with_concepts else ()
    paper = await session.get(Paper, paper_id, options=options)
    if paper is None:
        return None
    # P5c 公共方向库全实验室可读：论文在任一**公共**库有成员行时，任何登录用户可读；
    # 个人库（is_public=false）只对归属人放行，与 library_visible_to 的口径一致——
    # 否则别人私有个人库里的论文可被任意用户凭 paper_id 读到。
    # 视角取确定性成员行（最早入库的那份）；无课题上下文
    # （project_id=None：伴读不带参考检索、LLM 记账归个人）。
    from app.models.library_direction import DirectionLibrary

    shared_stmt = (
        select(LibraryPaper)
        .join(DirectionLibrary, DirectionLibrary.id == LibraryPaper.library_id)
        .where(
            LibraryPaper.paper_id == paper_id,
            or_(
                DirectionLibrary.is_public.is_(True),
                DirectionLibrary.submitted_by == user_id,
            ),
        )
        .order_by(LibraryPaper.created_at)
        .limit(1)
    )
    shared = (await session.execute(shared_stmt)).scalars().first()
    if shared is not None:
        return PaperView(paper, shared, None)
    stmt = (
        select(TopicPaper.topic_id)
        .join(ProjectMember, ProjectMember.project_id == TopicPaper.topic_id)
        .where(
            TopicPaper.paper_id == paper_id,
            TopicPaper.trashed_at.is_(None),  # 回收站里的书架行不再是可达链路
            ProjectMember.user_id == user_id,
        )
        .order_by(TopicPaper.created_at)
        .limit(1)
    )
    topic_id = (await session.execute(stmt)).scalar_one_or_none()
    if topic_id is None:
        entry = await user_library.entry_for_paper(session, user_id=user_id, paper=paper)
        if entry is None and not await _paper_in_daily_feed(session, paper_id):
            # 书架 / 个人库都不可达，也不在每日推送里 → 视为不存在
            return None
    membership = LibraryPaper(
        status="included", created_at=paper.created_at, updated_at=paper.updated_at
    )
    return PaperView(paper, membership, topic_id)


async def get_paper_for_user(
    session: AsyncSession,
    *,
    paper_id: uuid.UUID,
    user_id: uuid.UUID,
    with_concepts: bool = False,
    include_pool: bool = False,
) -> PaperView | None:
    """取论文（含成员行视角）；用户任一所属方向的库里都没有时视为不存在。

    论文同时在多个可见方向库时取一个确定性视角：最早加入的那个方向
    （解读不随方向变，跨库只影响 status / 相关性分这类判断字段）。
    include_pool=True（只给读路径用）时再走池级兜底：书架 / 个人库可达的
    无库论文也可读（见 :func:`_pool_paper_view`）。
    """
    stmt = (
        user_visible_paper_stmt(user_id)
        .where(Paper.id == paper_id)
        .order_by(LibraryPaper.created_at)
        .limit(1)
    )
    if with_concepts:
        stmt = stmt.options(selectinload(Paper.concepts))
    row = (await session.execute(stmt)).first()
    if row is not None:
        paper, membership, project_id = row
        return PaperView(paper, membership, project_id)
    if not include_pool:
        return None
    return await _pool_paper_view(
        session, paper_id=paper_id, user_id=user_id, with_concepts=with_concepts
    )


async def get_library_paper_view(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    project_id: uuid.UUID | None,
    paper_id: uuid.UUID,
    with_concepts: bool = False,
) -> PaperView | None:
    """取某篇论文在**指定库**的成员行视角（库工作台的单篇管理入口）。

    与 :func:`get_paper_for_user` 的确定性跨库归并不同：这里精确锁定
    (library_id, paper_id) 的成员行，保证库工作台的写操作只动本库那份归属。
    库不含该论文时返回 None。project_id = 库回指的课题（独立库为 None）。
    """
    stmt = member_paper_stmt(library_id).where(Paper.id == paper_id).limit(1)
    if with_concepts:
        stmt = stmt.options(selectinload(Paper.concepts))
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    paper, membership = row
    return PaperView(paper, membership, project_id)


async def set_paper_status(session: AsyncSession, view: PaperView, status: str) -> PaperView:
    view.membership.status = status
    await session.commit()
    await session.refresh(view.membership)
    return view


# ---- 标签 / 个人状态 / 笔记数聚合（docs/api-lit.md §5） ----


async def paper_extras_map(
    session: AsyncSession,
    *,
    paper_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID,
    library_ids: Sequence[uuid.UUID] | None = None,
) -> dict[uuid.UUID, dict[str, Any]]:
    """批量取论文的 tags / my_tags / starred / reading_status / note_count（聚合查询，避免 N+1）。

    - ``tags`` 是**库标签**（共享），只取本次浏览上下文 ``library_ids`` 里的那些：一篇论文
      可能同时在多个库，不限定就会把别的库打的标签串到这里来。没有库上下文（书架 /
      个人库 / 每日推送这类池级可达的论文）时不显示库标签——无从归属，也不该把别人库
      的整理口径漏出去。
    - ``my_tags`` 是请求者自己的个人标签（user_paper_tags），与库无关，任何上下文都显示。
    - ``note_count`` 是请求者本人的笔记数（P5b 起笔记 paper × author，仅作者可见）。
    """
    extras: dict[uuid.UUID, dict[str, Any]] = {
        pid: {
            "tags": [],
            "my_tags": [],
            "starred": False,
            "reading_status": "unread",
            "note_count": 0,
        }
        for pid in paper_ids
    }
    if not extras:
        return extras
    ids = list(extras.keys())
    if library_ids:
        tag_rows = await session.execute(
            select(paper_tag_links.c.paper_id, PaperTag.name)
            .join(PaperTag, PaperTag.id == paper_tag_links.c.tag_id)
            .where(paper_tag_links.c.paper_id.in_(ids), PaperTag.library_id.in_(library_ids))
            .order_by(PaperTag.name)
        )
        for pid, name in tag_rows.all():
            if name not in extras[pid]["tags"]:  # 多库上下文里同名标签只算一次
                extras[pid]["tags"].append(name)
    my_tag_rows = await session.execute(
        select(UserPaperTag.paper_id, UserPaperTag.name)
        .where(UserPaperTag.paper_id.in_(ids), UserPaperTag.user_id == user_id)
        .order_by(UserPaperTag.name)
    )
    for pid, name in my_tag_rows.all():
        extras[pid]["my_tags"].append(name)
    note_rows = await session.execute(
        select(PaperNote.paper_id, func.count())
        .where(PaperNote.paper_id.in_(ids), PaperNote.author_id == user_id)
        .group_by(PaperNote.paper_id)
    )
    for pid, count in note_rows.all():
        extras[pid]["note_count"] = int(count)
    meta_rows = await session.execute(
        select(PaperUserMeta).where(
            PaperUserMeta.paper_id.in_(ids), PaperUserMeta.user_id == user_id
        )
    )
    for meta in meta_rows.scalars():
        extras[meta.paper_id]["starred"] = meta.starred
        extras[meta.paper_id]["reading_status"] = meta.reading_status
    return extras


async def set_paper_tags(session: AsyncSession, view: PaperView, names: list[str]) -> list[str]:
    """整组覆盖论文标签：新名字自动建 tag，空数组=清空。返回排序后的标签名。

    P9e：标签作用域是文献库（PaperView 的成员行所属库），课题与独立库一视同仁。
    """
    library_id = view.membership.library_id
    paper_id = view.paper.id
    cleaned = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    existing = (
        (
            await session.execute(
                select(PaperTag).where(
                    PaperTag.library_id == library_id, PaperTag.name.in_(cleaned or [""])
                )
            )
        )
        .scalars()
        .all()
    )
    by_name = {t.name: t for t in existing}
    for name in cleaned:
        if name not in by_name:
            tag = PaperTag(library_id=library_id, name=name)
            session.add(tag)
            by_name[name] = tag
    await session.flush()
    await session.execute(delete(paper_tag_links).where(paper_tag_links.c.paper_id == paper_id))
    if cleaned:
        await session.execute(
            insert(paper_tag_links).values(
                [{"paper_id": paper_id, "tag_id": by_name[n].id} for n in cleaned]
            )
        )
    await prune_orphan_tags(session, library_id=library_id)
    await session.commit()
    return sorted(cleaned)


async def prune_orphan_tags(session: AsyncSession, *, library_id: uuid.UUID | None) -> int:
    """删除库内零引用标签（以 paper_tag_links 计数，回收站论文的引用也算），返回删除数。

    不 commit，由调用方在收尾时提交；触发点：整组覆盖标签、硬删论文、清空回收站。
    """
    stmt = delete(PaperTag).where(
        PaperTag.library_id == library_id,
        ~exists().where(paper_tag_links.c.tag_id == PaperTag.id),
    )
    result = await session.execute(stmt.execution_options(synchronize_session="fetch"))
    return int(result.rowcount or 0)


async def list_library_tags(
    session: AsyncSession, *, library_id: uuid.UUID
) -> list[dict[str, Any]]:
    """库标签列表（含引用论文数），按名称排序。"""
    rows = await session.execute(
        select(PaperTag.id, PaperTag.name, func.count(paper_tag_links.c.paper_id))
        .outerjoin(paper_tag_links, paper_tag_links.c.tag_id == PaperTag.id)
        .where(PaperTag.library_id == library_id)
        .group_by(PaperTag.id, PaperTag.name)
        .order_by(PaperTag.name)
    )
    return [{"id": tid, "name": name, "paper_count": int(count)} for tid, name, count in rows]


async def set_user_paper_tags(
    session: AsyncSession, *, paper_id: uuid.UUID, user_id: uuid.UUID, names: list[str]
) -> list[str]:
    """整组覆盖某人给某篇打的**个人标签**（空数组=清空），返回排序后的标签名。

    与库标签的整组覆盖（:func:`set_paper_tags`）互不影响：这里只删改
    (user_id, paper_id) 这一格，别人给同一篇打的个人标签、以及库标签都不动。
    名字内联存，删掉最后一处引用就没了，不需要额外的零引用回收。
    """
    cleaned = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    await session.execute(
        delete(UserPaperTag).where(
            UserPaperTag.paper_id == paper_id, UserPaperTag.user_id == user_id
        )
    )
    for name in cleaned:
        session.add(UserPaperTag(paper_id=paper_id, user_id=user_id, name=name))
    await session.commit()
    return sorted(cleaned)


async def list_user_paper_tags(
    session: AsyncSession, *, user_id: uuid.UUID
) -> list[dict[str, Any]]:
    """「我的所有标签」（含标了几篇），按名称排序——给前端的筛选下拉 / 输入建议用。"""
    rows = await session.execute(
        select(UserPaperTag.name, func.count(UserPaperTag.paper_id))
        .where(UserPaperTag.user_id == user_id)
        .group_by(UserPaperTag.name)
        .order_by(UserPaperTag.name)
    )
    return [{"name": name, "paper_count": int(count)} for name, count in rows]


async def upsert_paper_user_meta(
    session: AsyncSession,
    *,
    paper: Any,
    user_id: uuid.UUID,
    starred: bool | None = None,
    reading_status: str | None = None,
) -> PaperUserMeta:
    """个人星标 / 阅读状态 upsert（只更新提供的字段）。"""
    meta = (
        await session.execute(
            select(PaperUserMeta).where(
                PaperUserMeta.paper_id == paper.id, PaperUserMeta.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if meta is None:
        meta = PaperUserMeta(paper_id=paper.id, user_id=user_id)
        session.add(meta)
    if starred is not None:
        meta.starred = starred
    if reading_status is not None:
        meta.reading_status = reading_status
    await session.commit()
    await session.refresh(meta)
    return meta


# ---- 从方向库移除论文（docs/api-lit.md §8.6） ----
#
# P4 全局内容池语义：删除 = 删本方向的成员行与项目侧标签关联。内容池 Paper 行与磁盘
# 文件默认保留（可能被其他方向复用）；但当这是该论文最后一处引用（别的库/书架/个人库/
# 每日推送/论著都没有了）时，回收孤儿本体 + 落盘文件，避免「彻底删除」名不副实、重加秒
# 命中（见 gc_orphan_papers）。个人笔记/划线/分块向量等派生行随 Paper 级联清理。


async def _delete_membership_rows(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    memberships: Sequence[LibraryPaper],
) -> None:
    """硬删成员行 + 本库挂在这些论文上的标签关联（不 commit）。"""
    paper_ids = [m.paper_id for m in memberships]
    if not paper_ids:
        return
    library_tag_ids = select(PaperTag.id).where(PaperTag.library_id == library_id)
    await session.execute(
        delete(paper_tag_links).where(
            paper_tag_links.c.paper_id.in_(paper_ids),
            paper_tag_links.c.tag_id.in_(library_tag_ids),
        )
    )
    for membership in memberships:
        await session.delete(membership)
    await session.flush()


async def _paper_still_referenced(session: AsyncSession, paper: Paper) -> bool:
    """论文是否仍被任一「集合」引用——是则保留内容池本体。

    集合 = 方向库成员 / 课题书架 / 个人文献库(仅 saved 收藏) / 每日推送 / 论著引用。书架与
    个人库都只算「在架/在收藏」的行：回收站里的书架行（trashed_at 非空）不该让被删论文
    续命。个人库既看软引用 last_paper_id，也看 dedup_key（论文曾被删过重加时软引用可能已
    断）；但只算 saved=True 的真收藏——saved=False 是纯浏览记录（含回收站条目），不该让被
    删论文靠"看过一次"续命。派生数据（笔记/划线/分块向量/个人元数据/标签关联/图片记录）
    不算集合，随本体级联清理。
    """
    checks = (
        select(LibraryPaper.id).where(LibraryPaper.paper_id == paper.id),
        select(TopicPaper.id).where(
            TopicPaper.paper_id == paper.id, TopicPaper.trashed_at.is_(None)
        ),
        select(DailyFeedEntry.id).where(DailyFeedEntry.paper_id == paper.id),
        select(UserPublication.id).where(UserPublication.paper_id == paper.id),
        select(UserLibraryEntry.id).where(
            UserLibraryEntry.saved.is_(True),
            or_(
                UserLibraryEntry.last_paper_id == paper.id,
                UserLibraryEntry.dedup_key == paper.dedup_key,
            ),
        ),
    )
    for stmt in checks:
        if (await session.execute(stmt.limit(1))).first() is not None:
            return True
    return False


def _remove_paper_files(paper: Paper) -> None:
    """尽力删除论文落盘文件：pdf/全文文件 + <papers_dir>/<id>/ 图片目录（失败只记日志）。"""
    from app.services.literature.pdf_extract import papers_dir

    for raw in (paper.pdf_path, paper.full_text_path):
        if raw:
            try:
                Path(raw).unlink(missing_ok=True)
            except OSError:
                logger.warning("orphan paper file unlink failed: %s", raw, exc_info=True)
    shutil.rmtree(papers_dir() / str(paper.id), ignore_errors=True)


async def gc_orphan_papers(session: AsyncSession, paper_ids: Sequence[uuid.UUID]) -> int:
    """回收已成孤儿（不再被任何集合引用）的内容池论文：删本体 + 落盘文件，返回删除数。

    调用方须已先删除触发检查的那条引用（成员行 / 过期推送 entry 等）并 flush。DB 外键
    级联清理派生行（分块向量/笔记/划线/个人元数据/标签关联/图片记录）；本函数补删磁盘文件。
    """
    removed = 0
    for paper_id in set(paper_ids):
        paper = await session.get(Paper, paper_id)
        if paper is None or await _paper_still_referenced(session, paper):
            continue
        _remove_paper_files(paper)
        await session.delete(paper)
        removed += 1
    if removed:
        await session.flush()
    return removed


async def delete_membership_hard(session: AsyncSession, membership: LibraryPaper) -> None:
    """把一条成员行彻底删掉（不进回收站），并顺带清理孤儿标签与孤儿论文。

    自动淘汰用这条：相关性不足的论文每天上百篇，堆进回收站没人会去翻，还会把用户
    手动删的那几篇淹掉。论文本体只在**再没有任何集合引用它**时才回收——被每日池
    引用着的照常留在内容池里，别的库要用还能用。
    """
    library_id = membership.library_id
    paper_id = membership.paper_id
    await _delete_membership_rows(session, library_id=library_id, memberships=[membership])
    await prune_orphan_tags(session, library_id=library_id)
    await gc_orphan_papers(session, [paper_id])


async def delete_paper(session: AsyncSession, view: PaperView) -> None:
    """从当前方向库彻底移除一篇论文（回收站里的「彻底删除」）。

    删本库成员行与标签关联，收尾清理库内零引用标签；若这是该论文最后一处引用（别的
    库/书架/个人库/推送/论著都没有了），连内容池本体与落盘文件一并回收（孤儿清理）。
    """
    library_id = view.membership.library_id
    paper_id = view.membership.paper_id
    await _delete_membership_rows(session, library_id=library_id, memberships=[view.membership])
    await prune_orphan_tags(session, library_id=library_id)
    await gc_orphan_papers(session, [paper_id])
    await session.commit()


async def _delete_or_trash_memberships(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    paper_ids: list[uuid.UUID],
    hard: bool,
) -> int:
    """批量软删/硬删某库内成员行的共享实现（标签关联按库清理）。"""
    memberships = (
        (
            await session.execute(
                select(LibraryPaper).where(
                    LibraryPaper.library_id == library_id, LibraryPaper.paper_id.in_(paper_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    if hard:
        hard_paper_ids = [m.paper_id for m in memberships]
        await _delete_membership_rows(session, library_id=library_id, memberships=memberships)
        await prune_orphan_tags(session, library_id=library_id)
        await gc_orphan_papers(session, hard_paper_ids)
    else:
        for membership in memberships:
            membership.status = "excluded"
            membership.trash_reason = "manual"
    await session.commit()
    return len(memberships)


async def delete_papers(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    paper_ids: list[uuid.UUID],
    hard: bool = False,
) -> int:
    """批量删除项目库内论文（非本库的 id 忽略），返回处理数。

    默认软删（移入回收站 = 成员行 status excluded，可召回）；hard=True 删成员行。
    """
    # 删除/回收站是课题「自己那份库」的管理操作，落在起源库上（不动共享库）
    library = await get_library_for_project(session, project_id)
    if library is None:
        return 0
    return await _delete_or_trash_memberships(
        session,
        library_id=library.id,
        paper_ids=paper_ids,
        hard=hard,
    )


async def delete_library_papers(
    session: AsyncSession,
    *,
    library: Any,
    paper_ids: list[uuid.UUID],
    hard: bool = False,
) -> int:
    """批量删除某方向库内论文（库工作台入口，含独立库）。"""
    return await _delete_or_trash_memberships(
        session,
        library_id=library.id,
        paper_ids=paper_ids,
        hard=hard,
    )


def restore_status_of(view: PaperView) -> str:
    """回收站召回后的状态：已编译回 compiled；打过分回 scored；否则按人工精选处理。"""
    if view.has_wiki:
        return "compiled"
    if view.membership.relevance_score is not None:
        return "scored"
    return "included"


async def restore_paper(session: AsyncSession, view: PaperView) -> PaperView:
    """从回收站召回（docs/api-lit.md §8.6）。"""
    view.membership.status = restore_status_of(view)
    view.membership.trash_reason = None
    await session.commit()
    await session.refresh(view.membership)
    return view


async def _empty_trash_core(session: AsyncSession, *, library_id: uuid.UUID) -> int:
    """彻底移除某库全部 excluded 成员行（标签关联按库清理）。"""
    memberships = (
        (
            await session.execute(
                select(LibraryPaper).where(
                    LibraryPaper.library_id == library_id, LibraryPaper.status == "excluded"
                )
            )
        )
        .scalars()
        .all()
    )
    if memberships:
        trashed_paper_ids = [m.paper_id for m in memberships]
        await _delete_membership_rows(session, library_id=library_id, memberships=memberships)
        await prune_orphan_tags(session, library_id=library_id)
        await gc_orphan_papers(session, trashed_paper_ids)
    await session.commit()
    return len(memberships)


async def empty_trash(session: AsyncSession, *, project_id: uuid.UUID) -> int:
    """清空回收站：彻底移除库内全部 excluded 成员行，返回删除数。"""
    library = await get_library_for_project(session, project_id)
    if library is None:
        return 0
    return await _empty_trash_core(session, library_id=library.id)


async def empty_library_trash(session: AsyncSession, *, library: Any) -> int:
    """清空某方向库的回收站（库工作台入口，含独立库）。"""
    return await _empty_trash_core(session, library_id=library.id)


# ---- PDF 按需补下（docs/api-lit.md §1） ----


def _validate_pdf_content(content: bytes) -> None:
    """校验上传内容确实是至少含一页、无需密码的 PDF。"""
    if not content.startswith(b"%PDF-"):
        raise PdfUploadInvalidError("文件头不是 PDF")
    try:
        import pymupdf

        with pymupdf.open(stream=content, filetype="pdf") as doc:
            if doc.needs_pass:
                raise PdfUploadInvalidError("暂不支持加密 PDF")
            if doc.page_count < 1:
                raise PdfUploadInvalidError("PDF 没有页面")
    except PdfUploadInvalidError:
        raise
    except Exception as exc:
        raise PdfUploadInvalidError(f"PDF 无法读取：{exc}") from exc


async def _process_saved_pdf(
    session: AsyncSession,
    paper: Paper,
    pdf_path: Path,
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> Paper:
    """对已经落盘的 PDF 执行统一后处理：全文、分块、向量和机构。"""
    from app.services.literature.pdf_extract import extract_full_text

    paper.pdf_path = str(pdf_path)
    # 新上传/重新取得原件时不能沿用旧全文路径；抽取失败就明确退化为仅有 PDF。
    paper.full_text_path = None
    try:
        txt_path = await extract_full_text(str(paper.id), pdf_path)
        paper.full_text_path = str(txt_path)
    except Exception:  # noqa: BLE001 — 抽取失败降级：仅有 PDF、无全文
        logger.warning("full text extraction failed for paper %s", paper.id, exc_info=True)
    # 分段索引（文献问答底座）；失败不影响 PDF 落盘。抽到全文就按全文切（替换掉此前的
    # 摘要兜底块）；已有全文块则不重切，避免丢已补的块向量
    from app.services.chunks import ensure_paper_chunks

    try:
        await ensure_paper_chunks(session, paper)
    except Exception:  # noqa: BLE001
        logger.warning("chunk indexing failed for paper %s", paper.id, exc_info=True)
    # 论文级向量：激活空间下缺就补。best-effort，不影响 PDF 落盘
    from app.services.paper_enrich import embed_paper, has_current_paper_vector

    if not await has_current_paper_vector(session, paper):
        try:
            await embed_paper(session, paper, user_id=user_id, project_id=project_id)
        except Exception:  # noqa: BLE001 — provider 不支持嵌入等：降级为无向量
            logger.warning("paper embedding failed for paper %s", paper.id, exc_info=True)
    # 发表机构：on_add 模式下全文到手后 LLM 从标题页逐位作者解析机构（此路径原先不补
    # 机构）；on_compile 模式跳过，改由 wiki 编译折叠抽取。失败不影响主流程
    if not paper.affiliations and paper.full_text_path:
        try:
            from app.core.llm.router import get_llm_router
            from app.services.affiliations import (
                apply_author_affiliations,
                extract_author_affiliations_llm,
                get_affiliation_extraction_mode,
            )

            if await get_affiliation_extraction_mode(session) == "on_add":
                mapping = await extract_author_affiliations_llm(
                    paper, llm=get_llm_router(), user_id=user_id, project_id=project_id
                )
                apply_author_affiliations(paper, mapping)
        except Exception:  # noqa: BLE001
            logger.warning(
                "affiliation extraction failed for paper %s", paper.id, exc_info=True
            )
    await session.commit()
    # 摘要兜底块的向量 = 论文级向量的拷贝（零 token，不受任何开关控制）
    from app.services.chunks import sync_abstract_chunk_vectors

    try:
        if await sync_abstract_chunk_vectors(session, paper_ids=[paper.id]):
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("abstract chunk vector sync failed for %s", paper.id, exc_info=True)

    # 块向量：全文已经抓到了，向量就一定建。best-effort，内部自 commit
    from app.core.llm.router import get_llm_router
    from app.services.chunks import embed_pending_chunks_for_papers

    try:
        await embed_pending_chunks_for_papers(
            session,
            paper_ids=[paper.id],
            llm=get_llm_router(),
            user_id=user_id,
            project_id=project_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning("chunk embed failed for paper %s", paper.id, exc_info=True)
    await session.refresh(paper)
    return paper


async def upload_pdf(
    session: AsyncSession,
    paper: Paper,
    content: bytes,
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> Paper:
    """把用户提供的 PDF 接到现有论文，并走与自动下载相同的后处理流水线。"""
    from app.services.literature.pdf_extract import save_pdf

    if paper.pdf_path and Path(paper.pdf_path).exists():
        raise PdfAlreadyExistsError(str(paper.id))
    await asyncio.to_thread(_validate_pdf_content, content)
    pdf_path = save_pdf(str(paper.id), content)
    return await _process_saved_pdf(
        session, paper, pdf_path, user_id=user_id, project_id=project_id
    )


async def upload_pdf_from_url(
    session: AsyncSession,
    paper: Paper,
    url: str,
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> Paper:
    """按用户给的公开链接取 PDF，接到现有论文上。

    和本地上传共用同一套规矩：已有 PDF 就拒绝（不覆盖原件）、同样的内容校验、
    同一条后处理流水线。差别只在字节从哪里来——而那一段是有 SSRF 面的，
    单独放在 :mod:`app.services.literature.pdf_source` 里。
    """
    from app.services.literature.pdf_extract import save_pdf
    from app.services.literature.pdf_source import download_pdf

    # 先挡住已有 PDF 再下载：否则每被拒一次都白跑一趟外网。
    if paper.pdf_path and Path(paper.pdf_path).exists():
        raise PdfAlreadyExistsError(str(paper.id))
    content = await download_pdf(url)
    await asyncio.to_thread(_validate_pdf_content, content)
    pdf_path = save_pdf(str(paper.id), content)
    return await _process_saved_pdf(
        session, paper, pdf_path, user_id=user_id, project_id=project_id
    )


async def fetch_pdf(
    session: AsyncSession,
    paper: Paper,
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> Paper:
    """按需补下 PDF + 抽全文；已有 PDF 文件时幂等直接返回（只动内容池本体字段）。

    - 无 arxiv_id → PdfSourceUnsupportedError（路由映射 400）
    - 下载失败 → PdfFetchFailedError（路由映射 502）
    - 全文抽取失败只记日志，不影响 PDF 落盘
    """
    from app.services.literature import get_arxiv_client
    from app.services.literature.pdf_extract import save_pdf

    if paper.pdf_path and Path(paper.pdf_path).exists():
        return paper
    if not paper.arxiv_id:
        raise PdfSourceUnsupportedError("论文没有 arxiv 编号，暂不支持自动获取 PDF")
    try:
        content = await get_arxiv_client().download_pdf(paper.arxiv_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        raise PdfFetchFailedError(f"{type(e).__name__}: {e}") from e
    pdf_path = save_pdf(str(paper.id), content)
    return await _process_saved_pdf(
        session, paper, pdf_path, user_id=user_id, project_id=project_id
    )


# ---- AI 伴读上下文（docs/api-lit.md §3） ----


def build_chat_context(paper: PaperView) -> str:
    """伴读上下文：优先 full_text（超长头尾各留一半），否则 wiki_content，否则 abstract。"""
    if paper.full_text_path and Path(paper.full_text_path).exists():
        text_ = Path(paper.full_text_path).read_text(encoding="utf-8", errors="ignore")
        if len(text_) > CHAT_CONTEXT_MAX_CHARS:
            half = CHAT_CONTEXT_MAX_CHARS // 2
            text_ = f"{text_[:half]}\n\n……（论文太长，中间部分已省略）……\n\n{text_[-half:]}"
        return text_
    return paper.wiki_content or paper.abstract or ""


CHAT_SYSTEM_PROMPT_TEMPLATE = """\
你是论文阅读助手，帮用户读懂下面这篇论文。回答要求：
- 只依据下面给出的资料回答，不要编造资料里没有的信息；
- 资料里没有提到或你不确定的，直接说明「论文中未提及」或「不确定」；
- 用中文回答，讲清楚、说人话。

论文标题：{title}

论文内容：
{context}
"""

# 用户在 / 选择器里额外挑中的其他文献：拼在 system 末尾作为对比/参考资料。
CHAT_REFERENCES_SUFFIX = """

————
用户还选了下面这些【其他文献】作为对比/参考资料（编号 = 论文，仅为检索到的相关片段或摘要，非全文）。
需要对比或引用它们时依据这里的内容，并在句末用 [n] 标注来源；
这些资料没覆盖的细节，请说明「参考文献中未提及」：
{references}
"""


def build_chat_messages(
    paper: PaperView,
    *,
    question: str,
    history: Sequence[tuple[str, str]] = (),
    references: str = "",
) -> list[Message]:
    """组装伴读消息：system（论文上下文 + 可选参考文献）+ 历史对话（前端携带）+ 当前问题。"""
    system = CHAT_SYSTEM_PROMPT_TEMPLATE.format(
        title=paper.title, context=build_chat_context(paper)
    )
    if references:
        system += CHAT_REFERENCES_SUFFIX.format(references=references)
    messages = [Message(role="system", content=system)]
    messages += [Message(role=role, content=content) for role, content in history]
    messages.append(Message(role="user", content=question))
    return messages


# ---- 检索 ----


async def keyword_search_papers(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    #: 显式库集合（全局助手）：给了就不看 project_id
    library_ids: Sequence[uuid.UUID] | None = None,
    q: str,
    limit: int,
    user_id: uuid.UUID | None = None,
) -> list[tuple[PaperView, float]]:
    """关键词检索：title/abstract/解读正文/我的笔记内容 ilike，按命中位置给启发式分。

    只检索库内文献（相关性达标）：已删除（excluded）/未筛选（candidate）不出现。
    笔记仅作者本人可见（P5b），故只有传 user_id（用户检索入口）才并入笔记命中；
    agent 调用（无用户语境）不搜笔记。入口同 list_papers：project_id 或 library_id。
    """
    library_ids = await _read_library_ids(
        session, project_id=project_id, library_id=library_id, library_ids=library_ids
    )
    if not library_ids:
        return []
    pattern = f"%{q}%"
    hits = [
        Paper.title.ilike(pattern),
        Paper.abstract.ilike(pattern),
        Paper.id.in_(select(PaperWiki.paper_id).where(PaperWiki.content.ilike(pattern))),
    ]
    if user_id is not None:
        hits.append(
            Paper.id.in_(
                select(PaperNote.paper_id).where(
                    PaperNote.author_id == user_id, PaperNote.content.ilike(pattern)
                )
            )
        )
    stmt = (
        member_papers_stmt(library_ids)
        .where(LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]), or_(*hits))
        .limit(limit * 3 * len(library_ids))
    )
    rows = dedupe_member_rows((await session.execute(stmt)).all())
    needle = q.lower()

    def score_of(p: Paper) -> float:
        if needle in (p.title or "").lower():
            return 1.0
        if needle in (p.abstract or "").lower():
            return 0.7
        return 0.5  # wiki_content / 笔记命中

    ranked = sorted(
        ((PaperView(paper, membership, project_id), score_of(paper)) for paper, membership in rows),
        key=lambda x: -x[1],
    )
    return ranked[:limit]


async def keyword_search_concepts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    q: str,
    limit: int,
) -> list[tuple[Concept, float]]:
    library_ids = await _read_library_ids(session, project_id=project_id, library_id=library_id)
    if not library_ids:
        return []
    stmt = (
        select(Concept)
        .where(
            Concept.id.in_(concepts_service.library_concept_ids(library_ids)),
            Concept.name.ilike(f"%{q}%"),
        )
        .order_by(Concept.name)
        .limit(limit)
    )
    return [(c, 1.0) for c in (await session.execute(stmt)).scalars().all()]


def semantic_search_supported(session: AsyncSession) -> bool:
    return session.get_bind().dialect.name == "postgresql"


async def semantic_search_papers(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    #: 显式库集合（全局助手）：给了就不看 project_id
    library_ids: Sequence[uuid.UUID] | None = None,
    query_vector: list[float],
    space: EmbeddingSpace,
    limit: int,
) -> list[tuple[PaperView, float]]:
    """pgvector 余弦检索（仅 postgres；调用方需先判 semantic_search_supported）。

    只跟 ``space`` 这一个向量空间里的论文比较——别的空间的向量出自别的模型，
    余弦值没有可比性。
    """
    library_ids = await _read_library_ids(
        session, project_id=project_id, library_id=library_id, library_ids=library_ids
    )
    if not library_ids:
        return []
    qv = json.dumps(query_vector)
    # DISTINCT p.id：一篇论文命中多个关联库时只召回一次（分数不受成员行影响）。
    # status 过滤与 keyword_search_papers 对齐：回收站（excluded）和未筛选的候选
    # （candidate）都不该出现在检索结果里——删掉的论文又被搜出来，比搜不到更难理解。
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT p.id, 1 - (v.embedding <=> CAST(:qv AS vector)) AS score "
                "FROM paper_vectors v "
                "JOIN papers p ON p.id = v.paper_id "
                "JOIN library_papers lp ON lp.paper_id = p.id "
                "AND lp.library_id = ANY(CAST(:libs AS uuid[])) "
                "WHERE v.space = :space "
                "AND lp.status = ANY(CAST(:statuses AS varchar[])) "
                "ORDER BY score DESC "
                "LIMIT :k"
            ),
            {
                "qv": qv,
                "libs": [str(lid) for lid in library_ids],
                "k": limit,
                "space": space.key,
                "statuses": list(PAPER_STATUS_GROUPS["library"]),
            },
        )
    ).all()
    if not rows:
        return []
    scores = {row.id: float(row.score) for row in rows}
    pairs = dedupe_member_rows(
        (
            await session.execute(member_papers_stmt(library_ids).where(Paper.id.in_(list(scores))))
        ).all()
    )
    by_id = {p.id: PaperView(p, m, project_id) for p, m in pairs}
    return [(by_id[pid], scores[pid]) for pid in (r.id for r in rows) if pid in by_id]


def rerank_document_of(paper: Any) -> str:
    """重排送审文本：title + abstract，截断 RERANK_DOC_CHARS 字。"""
    text_ = paper.title or ""
    if paper.abstract:
        text_ = f"{text_}\n{paper.abstract}"
    return text_[:RERANK_DOC_CHARS]


async def rerank_paper_rows(
    llm_router: LLMRouter,
    *,
    query: str,
    rows: list[tuple[PaperView, float]],
    limit: int,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> tuple[list[tuple[PaperView, float]], bool]:
    """对向量召回结果做 rerank，返回 (top limit 结果, 是否重排成功)。

    rerank 未配置（NotImplementedError）或调用异常时降级：按原向量分取前 limit。
    """
    if not rows:
        return [], False
    documents = [rerank_document_of(p) for p, _ in rows]
    try:
        ranked = await llm_router.rerank(
            query, documents, top_n=limit, user_id=user_id, project_id=project_id
        )
    except Exception:  # 含 NotImplementedError：降级为纯向量分
        logger.warning("rerank failed, falling back to vector scores", exc_info=True)
        return rows[:limit], False
    return [(rows[i][0], score) for i, score in ranked[:limit]], True


async def collecting_libraries(
    session: AsyncSession, paper_id: uuid.UUID, user: Any
) -> list[dict[str, Any]]:
    """这篇论文被哪些**对该用户可见**的文献库收录了，带相关度分。

    只算真正进了库的状态（scored 及之后）——candidate 是还没打分、excluded 是打分没
    过，两者都不算"收录"，混进来会让人以为库里有这篇。

    可见性按 P10 口径过滤：个人库只对归属人与 admin 可见，不能经这个接口泄漏出去。
    """
    from app.models.library_direction import DirectionLibrary, LibraryPaper
    from app.services.libraries import library_visible_to

    rows = (
        await session.execute(
            select(DirectionLibrary, LibraryPaper.status, LibraryPaper.relevance_score)
            .join(LibraryPaper, LibraryPaper.library_id == DirectionLibrary.id)
            .where(
                LibraryPaper.paper_id == paper_id,
                LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
            )
            .order_by(LibraryPaper.relevance_score.desc().nulls_last())
        )
    ).all()
    return [
        {
            "library_id": lib.id,
            "name": lib.name,
            "is_public": lib.is_public,
            "status": status,
            "relevance_score": score,
        }
        for lib, status, score in rows
        if library_visible_to(lib, user)
    ]
