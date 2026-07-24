"""论文库与检索业务逻辑（不 import fastapi）。

P4 起 ``papers`` 是全局内容池：方向维度的归属/判断（status、相关性分、库版 wiki）
在 ``library_papers`` 成员行上。API 形状不变（仍收 project_id），这里解析到隐式库后
以 (Paper, LibraryPaper) 联查，并用 :class:`PaperView` 还原旧单表字段口径给 schema。
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

from sqlalchemy import Select, and_, cast, delete, exists, func, insert, or_, select, text
from sqlalchemy import Text as SAText
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
    paper_tag_links,
)
from app.models.project import ProjectMember
from app.models.publication import UserPublication
from app.models.topic_shelf import TopicPaper
from app.services.libraries import (
    dedupe_member_rows,
    get_library_for_project,
    get_source_library_ids,
    member_paper_stmt,
    member_papers_stmt,
    user_visible_paper_stmt,
)

logger = logging.getLogger(__name__)

PAPER_SORTS = ("relevance", "-published_at")

# 语义检索重排：向量召回候选数 / 送重排的文档截断长度
RERANK_CANDIDATES = 30
RERANK_DOC_CHARS = 512

# status 组别名：可见（检索到的全部，不含垃圾桶）/ 库内（相关性达标及之后）/
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


class PaperView:
    """内容池论文 + 库成员行的合并视角（字段口径与旧单表 Paper 一致）。

    本体字段（id/title/authors/pdf_path/…）透传 Paper；判断字段
    （status/relevance_score/wiki_content/…）取成员行；``project_id`` 为本次
    访问解析出的方向（过渡期 = 成员行所属隐式库回指的 project）。
    ``created_at`` 口径 = 加入本方向库的时间（成员行 created_at）。
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
        return self.membership.compiled_at

    @property
    def compiled_model(self) -> str | None:
        return self.membership.compiled_model

    @property
    def wiki_content(self) -> str | None:
        return self.membership.wiki_content

    @property
    def has_wiki(self) -> bool:
        return bool(self.membership.wiki_content)

    @property
    def created_at(self) -> datetime:
        return self.membership.created_at


async def _read_library_ids(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    library_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    """并集读路径的库解析：显式 library_id（单库读视图/库工作台）→ [library_id]；
    否则按课题关联库并集（P7；空关联=空语料，调用方返回空态而非报错）。"""
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
    starred: bool | None = None,
    reading_status: str | None = None,
    user_id: uuid.UUID | None = None,
    author: str | None = None,
    affiliation: str | None = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
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
    if tag and library_ids:
        stmt = stmt.where(
            Paper.id.in_(
                select(paper_tag_links.c.paper_id)
                .join(PaperTag, PaperTag.id == paper_tag_links.c.tag_id)
                .where(PaperTag.library_id.in_(library_ids), PaperTag.name == tag)
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
    status: str | None = None,
    q: str | None = None,
    tag: str | None = None,
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
) -> tuple[Sequence[PaperView], int]:
    """库内论文列表。入口二选一：library_id（单库读视图/库工作台）或 project_id
    （课题成员视角 = 关联库并集，P7）。project_id 兼作 PaperView 的课题上下文回填。

    单库（含课题只关联一个库的常见情形）走 SQL 分页快路径；课题关联多库时跨库
    同一论文按确定性视角归并（有 wiki 优先），Python 侧排序 + 分页保证可移植。"""
    library_ids = await _read_library_ids(session, project_id=project_id, library_id=library_id)
    if not library_ids:
        return [], 0

    filter_kwargs = dict(
        library_ids=library_ids,
        status=status,
        q=q,
        tag=tag,
        starred=starred,
        reading_status=reading_status,
        user_id=user_id,
        author=author,
        affiliation=affiliation,
        published_from=published_from,
        published_to=published_to,
        created_from=created_from,
        created_to=created_to,
    )

    if len(library_ids) == 1:
        stmt = apply_paper_filters(member_paper_stmt(library_ids[0]), **filter_kwargs)
        total = (await session.execute(stmt.with_only_columns(func.count()))).scalar_one()
        if sort == "-published_at":
            stmt = stmt.order_by(
                Paper.published_at.desc().nulls_last(), LibraryPaper.created_at.desc()
            )
        else:  # relevance（默认）
            stmt = stmt.order_by(
                LibraryPaper.relevance_score.desc().nulls_last(), LibraryPaper.created_at.desc()
            )
        stmt = stmt.offset((page - 1) * size).limit(size)
        rows = (await session.execute(stmt)).all()
        return [PaperView(paper, membership, project_id) for paper, membership in rows], int(total)

    # 关联多库并集：过滤 → 跨库归并 → Python 排序 + 分页
    stmt = apply_paper_filters(member_papers_stmt(library_ids), **filter_kwargs)
    all_rows = dedupe_member_rows((await session.execute(stmt)).all())
    if sort == "-published_at":
        all_rows.sort(
            key=lambda pm: (
                pm[0].published_at is None,
                -(pm[0].published_at.timestamp() if pm[0].published_at else 0.0),
                -pm[1].created_at.timestamp(),
            )
        )
    else:  # relevance（默认）
        all_rows.sort(
            key=lambda pm: (
                -(pm[1].relevance_score if pm[1].relevance_score is not None else -1e18),
                -pm[1].created_at.timestamp(),
            )
        )
    total = len(all_rows)
    start = (page - 1) * size
    page_rows = all_rows[start : start + size]
    return [PaperView(paper, membership, project_id) for paper, membership in page_rows], int(total)


async def _pool_paper_view(
    session: AsyncSession, *, paper_id: uuid.UUID, user_id: uuid.UUID, with_concepts: bool
) -> PaperView | None:
    """池级可见性兜底（P5b）：论文不在任何可见方向库，但个人链路可达时仍可读。

    可达条件：该论文在请求者任一课题的相关研究书架上，或在其个人库条目里
    （dedup 匹配）。返回的视角带**临时成员行**（不入 session、永不落库）：
    status=included、无判断字段；``project_id`` 取最早入架的课题（仅个人库
    可达时为 None）。只用于读路径——写成员行的端点不开启池级兜底。
    """
    from app.models.topic_shelf import TopicPaper
    from app.services import user_library

    options = (selectinload(Paper.concepts),) if with_concepts else ()
    paper = await session.get(Paper, paper_id, options=options)
    if paper is None:
        return None
    # P5c 方向库全实验室可读：论文只要在任一方向库有成员行，任何登录用户可读。
    # 视角取确定性成员行（优先有 wiki 解读的，其次最早入库的）；无课题上下文
    # （project_id=None：伴读不带参考检索、LLM 记账归个人）。
    shared_stmt = (
        select(LibraryPaper)
        .where(LibraryPaper.paper_id == paper_id)
        .order_by(LibraryPaper.wiki_content.is_(None), LibraryPaper.created_at)
        .limit(1)
    )
    shared = (await session.execute(shared_stmt)).scalars().first()
    if shared is not None:
        return PaperView(paper, shared, None)
    stmt = (
        select(TopicPaper.topic_id)
        .join(ProjectMember, ProjectMember.project_id == TopicPaper.topic_id)
        .where(TopicPaper.paper_id == paper_id, ProjectMember.user_id == user_id)
        .order_by(TopicPaper.created_at)
        .limit(1)
    )
    topic_id = (await session.execute(stmt)).scalar_one_or_none()
    if topic_id is None:
        entry = await user_library.entry_for_paper(session, user_id=user_id, paper=paper)
        if entry is None:
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

    论文同时在多个可见方向库时取一个确定性视角：优先有 wiki 解读的成员行，
    其次最早加入的（跨方向复用的论文以先入库方向的解读为主视角）。
    include_pool=True（只给读路径用）时再走池级兜底：书架 / 个人库可达的
    无库论文也可读（见 :func:`_pool_paper_view`）。
    """
    stmt = (
        user_visible_paper_stmt(user_id)
        .where(Paper.id == paper_id)
        .order_by(LibraryPaper.wiki_content.is_(None), LibraryPaper.created_at)
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
    session: AsyncSession, *, paper_ids: Sequence[uuid.UUID], user_id: uuid.UUID
) -> dict[uuid.UUID, dict[str, Any]]:
    """批量取论文的 tags / starred / reading_status / note_count（3 条聚合查询，避免 N+1）。

    note_count 是请求者本人的笔记数（P5b 起笔记 paper × author，仅作者可见）。"""
    extras: dict[uuid.UUID, dict[str, Any]] = {
        pid: {"tags": [], "starred": False, "reading_status": "unread", "note_count": 0}
        for pid in paper_ids
    }
    if not extras:
        return extras
    ids = list(extras.keys())
    tag_rows = await session.execute(
        select(paper_tag_links.c.paper_id, PaperTag.name)
        .join(PaperTag, PaperTag.id == paper_tag_links.c.tag_id)
        .where(paper_tag_links.c.paper_id.in_(ids))
        .order_by(PaperTag.name)
    )
    for pid, name in tag_rows.all():
        extras[pid]["tags"].append(name)
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

    集合 = 方向库成员 / 课题书架 / 个人文献库(仅 saved 收藏) / 每日推送 / 论著引用。个人库
    既看软引用 last_paper_id，也看 dedup_key（论文曾被删过重加时软引用可能已断）；但只算
    saved=True 的真收藏——saved=False 是纯浏览记录，不该让被删论文靠"看过一次"续命。派生
    数据（笔记/划线/分块向量/个人元数据/标签关联/图片记录）不算集合，随本体级联清理。
    """
    checks = (
        select(LibraryPaper.id).where(LibraryPaper.paper_id == paper.id),
        select(TopicPaper.id).where(TopicPaper.paper_id == paper.id),
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


async def delete_paper(session: AsyncSession, view: PaperView) -> None:
    """从当前方向库彻底移除一篇论文（垃圾桶里的「彻底删除」）。

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

    默认软删（移入垃圾桶 = 成员行 status excluded，可召回）；hard=True 删成员行。
    """
    # 删除/垃圾桶是课题「自己那份库」的管理操作，落在起源库上（不动共享库）
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


def restore_status_of(membership: LibraryPaper) -> str:
    """垃圾桶召回后的状态：已编译回 compiled；打过分回 scored；否则按人工精选处理。"""
    if membership.wiki_content:
        return "compiled"
    if membership.relevance_score is not None:
        return "scored"
    return "included"


async def restore_paper(session: AsyncSession, view: PaperView) -> PaperView:
    """从垃圾桶召回（docs/api-lit.md §8.6）。"""
    view.membership.status = restore_status_of(view.membership)
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
    """清空垃圾桶：彻底移除库内全部 excluded 成员行，返回删除数。"""
    library = await get_library_for_project(session, project_id)
    if library is None:
        return 0
    return await _empty_trash_core(session, library_id=library.id)


async def empty_library_trash(session: AsyncSession, *, library: Any) -> int:
    """清空某方向库的垃圾桶（库工作台入口，含独立库）。"""
    return await _empty_trash_core(session, library_id=library.id)


# ---- PDF 按需补下（docs/api-lit.md §1） ----


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
    from app.services.literature.pdf_extract import extract_full_text, save_pdf

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
    paper.pdf_path = str(pdf_path)
    try:
        txt_path = await extract_full_text(str(paper.id), pdf_path)
        paper.full_text_path = str(txt_path)
    except Exception:  # noqa: BLE001 — 抽取失败降级：仅有 PDF、无全文
        logger.warning("full text extraction failed for paper %s", paper.id, exc_info=True)
    # 全文分段索引（文献问答底座）；失败不影响 PDF 落盘
    if paper.full_text_path:
        from app.services.chunks import index_paper_fulltext

        try:
            await index_paper_fulltext(session, paper)
        except Exception:  # noqa: BLE001
            logger.warning("chunk indexing failed for paper %s", paper.id, exc_info=True)
    # 发表机构：on_add 模式下全文到手后 LLM 从标题页逐位作者解析机构（此路径原先不补
    # 机构）；on_compile 模式跳过，改由 wiki 编译折叠抽取。失败不影响主流程
    if not paper.affiliations and paper.full_text_path:
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
    await session.commit()
    await session.refresh(paper)
    return paper


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
    q: str,
    limit: int,
    user_id: uuid.UUID | None = None,
) -> list[tuple[PaperView, float]]:
    """关键词检索：title/abstract/库版 wiki/我的笔记内容 ilike，按命中位置给启发式分。

    只检索库内文献（相关性达标）：已删除（excluded）/未筛选（candidate）不出现。
    笔记仅作者本人可见（P5b），故只有传 user_id（用户检索入口）才并入笔记命中；
    agent 调用（无用户语境）不搜笔记。入口同 list_papers：project_id 或 library_id。
    """
    library_ids = await _read_library_ids(session, project_id=project_id, library_id=library_id)
    if not library_ids:
        return []
    pattern = f"%{q}%"
    hits = [
        Paper.title.ilike(pattern),
        Paper.abstract.ilike(pattern),
        LibraryPaper.wiki_content.ilike(pattern),
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
        .where(Concept.library_id.in_(library_ids), Concept.name.ilike(f"%{q}%"))
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
    query_vector: list[float],
    limit: int,
) -> list[tuple[PaperView, float]]:
    """pgvector 余弦检索（仅 postgres；调用方需先判 semantic_search_supported）。"""
    library_ids = await _read_library_ids(session, project_id=project_id, library_id=library_id)
    if not library_ids:
        return []
    qv = json.dumps(query_vector)
    # DISTINCT p.id：一篇论文命中多个关联库时只召回一次（分数不受成员行影响）
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT p.id, 1 - (p.embedding <=> CAST(:qv AS vector)) AS score "
                "FROM papers p "
                "JOIN library_papers lp ON lp.paper_id = p.id "
                "AND lp.library_id = ANY(CAST(:libs AS uuid[])) "
                "WHERE p.embedding IS NOT NULL "
                "ORDER BY score DESC "
                "LIMIT :k"
            ),
            {"qv": qv, "libs": [str(lid) for lid in library_ids], "k": limit},
        )
    ).all()
    if not rows:
        return []
    scores = {row.id: float(row.score) for row in rows}
    pairs = dedupe_member_rows(
        (
            await session.execute(
                member_papers_stmt(library_ids).where(Paper.id.in_(list(scores)))
            )
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
