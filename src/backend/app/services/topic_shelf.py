"""课题「相关研究」书架业务逻辑（P5a，不 import fastapi）。

三条铁律（docs-dev/workspace-ia-redesign.md §3.4/§3.6）：
- 论文本体纯引用（paper_id 指向全局内容池）；
- 库版 wiki 引用为主、入架落快照兜底：展示优先级 库版实时 > 个人编译版 >
  快照（P5b 三层解析；个人版 = 请求者本人 user_library_entries.wiki_content）；
- 入架必入个人库（user_library_entries，saved=true，共享同一次快照写入）；
  移出书架不动个人库。
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper
from app.models.topic_shelf import TopicPaper
from app.services import paper_import, user_library
from app.services.dedup import dedup_key_for
from app.services.libraries import find_pool_paper
from app.services.literature.arxiv import normalize_arxiv_id
from app.services.papers import apply_paper_filters


class PaperNotFoundError(Exception):
    """paper_id 在内容池中不存在。"""


class ShelfItemNotFoundError(Exception):
    """课题书架上没有这篇论文。"""


class NoWikiSourceError(Exception):
    """刷新快照时没有任何可用的 wiki 来源（库版 / 个人版都没有）。"""


class _SnapshotPaper:
    """给个人库快照用的论文视图：本体字段透传 Paper + 书架解析出的 wiki。"""

    __slots__ = ("_paper", "wiki_content")

    def __init__(self, paper: Paper, wiki_content: str | None) -> None:
        self._paper = paper
        self.wiki_content = wiki_content

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_paper"), name)


async def _wiki_rows_for(
    session: AsyncSession, paper_ids: list[uuid.UUID]
) -> list[Any]:
    """论文在各方向库的成员行概览：(paper_id, library_id, project_id, wiki_content)。"""
    if not paper_ids:
        return []
    stmt = (
        select(
            LibraryPaper.paper_id,
            LibraryPaper.library_id,
            DirectionLibrary.project_id,
            LibraryPaper.wiki_content,
        )
        .join(DirectionLibrary, DirectionLibrary.id == LibraryPaper.library_id)
        .where(LibraryPaper.paper_id.in_(paper_ids))
    )
    return list((await session.execute(stmt)).all())


def _pick_live_wiki(rows: list[Any], project_id: uuid.UUID) -> str | None:
    """从成员行里挑当前可得的库版 wiki：本课题隐式库优先，其次任一有 wiki 的库。"""
    own = next((r for r in rows if r.project_id == project_id and r.wiki_content), None)
    if own is not None:
        return own.wiki_content
    other = next((r for r in rows if r.wiki_content), None)
    return other.wiki_content if other is not None else None


def _item_dict(
    row: TopicPaper, paper: Paper, live_wiki: str | None, personal_wiki: str | None
) -> dict[str, Any]:
    """书架条目出参：wiki 展示优先级 库版实时 > 个人版 > 快照；source 标注来源状态。

    个人库条目的 wiki 字段身兼两职（个人编译版 / 浏览与入架时的库版快照），
    与书架快照内容相同时按快照标注（带日期更诚实），不同才算「个人版」。"""
    if live_wiki is not None:
        wiki_source, wiki_content = "live", live_wiki
    elif personal_wiki is not None and personal_wiki != row.wiki_snapshot:
        wiki_source, wiki_content = "personal", personal_wiki
    elif row.wiki_snapshot:
        wiki_source, wiki_content = "snapshot", row.wiki_snapshot
    else:
        wiki_source, wiki_content = "none", None
    return {
        "paper_id": paper.id,
        "title": paper.title,
        "authors": paper.authors or [],
        "year": paper.year,
        "venue": paper.venue,
        "arxiv_id": paper.arxiv_id,
        "doi": paper.doi,
        "url": paper.url,
        "tldr": paper.tldr,
        "note": row.note,
        "wiki_source": wiki_source,
        "wiki_content": wiki_content,
        "snapshot_at": row.snapshot_at,
        "source_library_id": row.source_library_id,
        "added_at": row.created_at,
    }


async def _get_row(
    session: AsyncSession, *, project_id: uuid.UUID, paper_id: uuid.UUID
) -> TopicPaper | None:
    stmt = select(TopicPaper).where(
        TopicPaper.topic_id == project_id, TopicPaper.paper_id == paper_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _item_of(
    session: AsyncSession,
    row: TopicPaper,
    paper: Paper,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    wiki_rows = await _wiki_rows_for(session, [paper.id])
    personal = await user_library.personal_wiki_map(session, user_id=user_id, papers=[paper])
    return _item_dict(
        row, paper, _pick_live_wiki(wiki_rows, project_id), personal.get(paper.id)
    )


async def list_shelf(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    page: int = 1,
    size: int = 20,
    q: str | None = None,
    author: str | None = None,
    affiliation: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    reading_status: str | None = None,
    starred: bool | None = None,
    sort: str = "added",
) -> tuple[list[dict[str, Any]], int]:
    """分页列书架，每条带解析后的 wiki 内容与来源状态。

    个人版层按请求者（user_id）本人的个人库条目解析。高级检索的
    q/author/affiliation/starred/reading_status 复用 :func:`apply_paper_filters`
    （只作用于内容池 Paper / 个人视角 PaperUserMeta，不触碰方向库）；
    year 范围就地作用于 ``Paper.year``。sort：added（默认，最新入架在前）/
    year / relevance / title。"""
    base = (
        select(TopicPaper, Paper)
        .join(Paper, Paper.id == TopicPaper.paper_id)
        .where(TopicPaper.topic_id == project_id)
    )
    # 复用论文库过滤器：传 None 的库相关参数（status/created_*）不会引用/join 方向库，
    # 故过滤只作用于 Paper 本体与请求者个人视角（PaperUserMeta），书架保持方向无关。
    base = apply_paper_filters(
        base,
        project_id=None,
        status=None,
        q=q,
        author=author,
        affiliation=affiliation,
        starred=starred,
        reading_status=reading_status,
        user_id=user_id,
        created_from=None,
        created_to=None,
    )
    if year_from is not None:
        base = base.where(Paper.year.isnot(None), Paper.year >= year_from)
    if year_to is not None:
        base = base.where(Paper.year.isnot(None), Paper.year <= year_to)

    total = (await session.execute(base.with_only_columns(func.count()))).scalar_one()

    if sort == "year":
        order = (Paper.year.desc().nulls_last(), TopicPaper.created_at.desc())
    elif sort == "title":
        order = (Paper.title.asc(),)
    elif sort == "relevance":
        # 相关性分在方向库成员行上（Paper 本体没有）；用只读相关子查询取该论文
        # 在各方向库的最高分排序，不 join 方向库、不改变书架行形状。
        relevance_sub = (
            select(func.max(LibraryPaper.relevance_score))
            .where(LibraryPaper.paper_id == Paper.id)
            .scalar_subquery()
        )
        order = (relevance_sub.desc().nulls_last(), TopicPaper.created_at.desc())
    else:  # added（默认，最新入架在前）
        order = (TopicPaper.created_at.desc(),)

    rows = (
        await session.execute(
            base.order_by(*order).offset((page - 1) * size).limit(size)
        )
    ).all()
    wiki_rows = await _wiki_rows_for(session, [paper.id for _, paper in rows])
    by_paper: dict[uuid.UUID, list[Any]] = {}
    for r in wiki_rows:
        by_paper.setdefault(r.paper_id, []).append(r)
    personal = await user_library.personal_wiki_map(
        session, user_id=user_id, papers=[paper for _, paper in rows]
    )
    items = [
        _item_dict(
            row,
            paper,
            _pick_live_wiki(by_paper.get(paper.id, []), project_id),
            personal.get(paper.id),
        )
        for row, paper in rows
    ]
    return items, int(total)


async def shelf_paper_ids(session: AsyncSession, *, project_id: uuid.UUID) -> list[uuid.UUID]:
    """书架全部 paper_id（前端标记「已入架」勾选态用）。"""
    stmt = (
        select(TopicPaper.paper_id)
        .where(TopicPaper.topic_id == project_id)
        .order_by(TopicPaper.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def add_to_shelf(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    user_id: uuid.UUID,
    note: str | None = None,
) -> dict[str, Any]:
    """入架：落 wiki 快照 + 同步 upsert 个人库（saved，共享同一次快照）。

    重复入架幂等：只更新 note（快照保持首次入架时的版本）。
    """
    paper = await session.get(Paper, paper_id)
    if paper is None:
        raise PaperNotFoundError(str(paper_id))
    row = await _get_row(session, project_id=project_id, paper_id=paper_id)
    if row is not None:
        if note is not None:
            row.note = note
        await session.commit()
        return await _item_of(session, row, paper, project_id, user_id)

    wiki_rows = await _wiki_rows_for(session, [paper_id])
    live_wiki = _pick_live_wiki(wiki_rows, project_id)
    # 来源库溯源：本课题隐式库优先，其次提供快照 wiki 的库，再其次任一库；都没有 = 个人补充
    own = next((r for r in wiki_rows if r.project_id == project_id), None)
    with_wiki = next((r for r in wiki_rows if r.wiki_content), None)
    source = own or with_wiki or (wiki_rows[0] if wiki_rows else None)
    row = TopicPaper(
        topic_id=project_id,
        paper_id=paper_id,
        source_library_id=source.library_id if source is not None else None,
        wiki_snapshot=live_wiki,
        snapshot_at=utcnow() if live_wiki is not None else None,
        note=note,
        added_by=user_id,
    )
    session.add(row)
    await session.flush()
    # 入架必入个人库（书架是个人库的课题投影）；save_paper 内部 commit 一并落书架行
    await user_library.save_paper(
        session, user_id=user_id, paper=_SnapshotPaper(paper, live_wiki)
    )
    return await _item_of(session, row, paper, project_id, user_id)


async def update_note(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    user_id: uuid.UUID,
    note: str | None,
) -> dict[str, Any]:
    row = await _get_row(session, project_id=project_id, paper_id=paper_id)
    if row is None:
        raise ShelfItemNotFoundError(str(paper_id))
    row.note = note
    await session.commit()
    paper = await session.get(Paper, paper_id)
    assert paper is not None  # 书架行外键保证
    return await _item_of(session, row, paper, project_id, user_id)


async def refresh_snapshot(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    """手动刷新书架快照：从当前可得的最优 wiki（库版 > 个人版）重拷。

    两个来源都没有 → NoWikiSourceError（路由映射 409）。"""
    row = await _get_row(session, project_id=project_id, paper_id=paper_id)
    if row is None:
        raise ShelfItemNotFoundError(str(paper_id))
    paper = await session.get(Paper, paper_id)
    assert paper is not None  # 书架行外键保证
    live = _pick_live_wiki(await _wiki_rows_for(session, [paper_id]), project_id)
    personal = await user_library.personal_wiki_map(session, user_id=user_id, papers=[paper])
    best = live or personal.get(paper_id)
    if best is None:
        raise NoWikiSourceError(str(paper_id))
    row.wiki_snapshot = best
    row.snapshot_at = utcnow()
    await session.commit()
    return await _item_of(session, row, paper, project_id, user_id)


async def remove_from_shelf(
    session: AsyncSession, *, project_id: uuid.UUID, paper_id: uuid.UUID
) -> None:
    """移出书架：只删书架行；个人库条目与内容池行都不动。"""
    row = await _get_row(session, project_id=project_id, paper_id=paper_id)
    if row is None:
        raise ShelfItemNotFoundError(str(paper_id))
    await session.delete(row)
    await session.commit()


async def import_to_shelf(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    arxiv_id: str | None = None,
    doi: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """个人补充入库：先按 dedup 查全局池，命中直接入架；未命中抓取解析入池后入架。

    个人补充**不建任何 library_papers 成员行**（「在池但不在任何库」是合法状态，
    §3.5）；解析计费按现有归因规则记调用用户。仅有标题且池中查不到时无法抓取
    （paper_import.ParseFailedError → 路由映射 422）。
    """
    normalized_arxiv = normalize_arxiv_id(arxiv_id) if arxiv_id else None
    clean_doi = doi.strip().removeprefix("https://doi.org/") if doi else None
    paper = await find_pool_paper(
        session,
        arxiv_id=normalized_arxiv,
        doi=clean_doi,
        dedup_key=dedup_key_for(arxiv_id=normalized_arxiv, doi=clean_doi, title=title),
    )
    if paper is None and title and title.strip():
        # 标题兜底：池键掺年份/首作者，纯标题哈希未必命中 → 退回大小写不敏感精确匹配
        stmt = select(Paper).where(func.lower(Paper.title) == title.strip().lower()).limit(1)
        paper = (await session.execute(stmt)).scalars().first()
    if paper is None:
        if not (normalized_arxiv or clean_doi):
            raise paper_import.ParseFailedError(
                "按标题没有找到这篇论文，请提供 arXiv 编号或 DOI"
            )
        fields = await paper_import.resolve_fields(arxiv_id=arxiv_id, doi=doi)
        # 解析出的规范 id 再查一次池（输入可能是版本号/别名）
        paper = await find_pool_paper(
            session,
            arxiv_id=fields.get("arxiv_id"),
            doi=fields.get("doi"),
            dedup_key=dedup_key_for(
                arxiv_id=fields.get("arxiv_id"),
                doi=fields.get("doi"),
                title=fields.get("title"),
                year=fields.get("year"),
                authors=fields.get("authors"),
            ),
        )
        if paper is None:
            paper = await paper_import.create_pool_paper(
                session, fields=fields, user_id=user_id, project_id=project_id
            )
    return await add_to_shelf(
        session, project_id=project_id, paper_id=paper.id, user_id=user_id
    )
