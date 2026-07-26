"""课题「相关研究」书架业务逻辑（P5a，不 import fastapi）。

三条铁律（docs-dev/workspace-ia-redesign.md §3.4/§3.6）：
- 论文本体纯引用（paper_id 指向全局内容池）；
- 解读纯引用：读 ``paper_wikis``（每篇论文全平台一份），查不到就是没有解读——
  不再有「库版 / 个人版 / 快照」的优先级链；
- 入架必入个人库（user_library_entries，saved=true）；移出书架不动个人库。

移出书架是软删（trashed_at）：进课题回收站，可召回 / 彻底删除 / 清空。唯一键
(topic_id, paper_id) 覆盖软删行，所以再次入架走「复活」而不是插新行。
"""

import uuid
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper
from app.models.topic_shelf import TopicPaper
from app.services import paper_import, user_library
from app.services.papers import apply_paper_filters


class PaperNotFoundError(Exception):
    """paper_id 在内容池中不存在。"""


class ShelfItemNotFoundError(Exception):
    """课题书架上没有这篇论文。"""


def _item_dict(row: TopicPaper, paper: Paper) -> dict[str, Any]:
    """书架条目出参（解读取论文级唯一那份，没有则为 null）。"""
    return {
        "paper_id": paper.id,
        "title": paper.title,
        "authors": paper.authors or [],
        "affiliations": paper.affiliations or [],
        "year": paper.year,
        "venue": paper.venue,
        "arxiv_id": paper.arxiv_id,
        "doi": paper.doi,
        "url": paper.url,
        "tldr": paper.tldr,
        "note": row.note,
        "has_wiki": paper.wiki is not None,
        "wiki_content": paper.wiki_content,
        "source_library_id": row.source_library_id,
        "added_at": row.created_at,
        "trashed_at": row.trashed_at,
    }


async def _get_row(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    trashed: bool | None = False,
) -> TopicPaper | None:
    """取书架行：trashed=False 只取在架的、True 只取回收站里的、None 两者都取。"""
    stmt = select(TopicPaper).where(
        TopicPaper.topic_id == project_id, TopicPaper.paper_id == paper_id
    )
    if trashed is True:
        stmt = stmt.where(TopicPaper.trashed_at.is_not(None))
    elif trashed is False:
        stmt = stmt.where(TopicPaper.trashed_at.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()


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
    my_tag: str | None = None,
    sort: str = "added",
    trashed: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """分页列书架，每条带这篇论文的解读（没有则为 null）。

    高级检索的
    q/author/affiliation/starred/reading_status/my_tag 复用 :func:`apply_paper_filters`
    （只作用于内容池 Paper / 个人视角 PaperUserMeta、UserPaperTag，不触碰方向库）；
    year 范围就地作用于 ``Paper.year``。sort：added（默认，最新入架在前）/
    year / relevance / title。trashed=True 列回收站（固定按移出时间倒序）。"""
    base = (
        select(TopicPaper, Paper)
        .join(Paper, Paper.id == TopicPaper.paper_id)
        .where(
            TopicPaper.topic_id == project_id,
            TopicPaper.trashed_at.is_not(None) if trashed else TopicPaper.trashed_at.is_(None),
        )
    )
    # 复用论文库过滤器：传 None 的库相关参数（status/created_*）不会引用/join 方向库，
    # 故过滤只作用于 Paper 本体与请求者个人视角（PaperUserMeta / UserPaperTag），
    # 书架保持方向无关（my_tag 是个人标签，同样不依赖库）。
    base = apply_paper_filters(
        base,
        status=None,
        q=q,
        author=author,
        affiliation=affiliation,
        starred=starred,
        reading_status=reading_status,
        my_tag=my_tag,
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
    if trashed:  # 回收站只按移出时间倒序（最近移出在前）
        order = (TopicPaper.trashed_at.desc(),)

    rows = (
        await session.execute(
            base.order_by(*order).offset((page - 1) * size).limit(size)
        )
    ).all()
    return [_item_dict(row, paper) for row, paper in rows], int(total)


async def shelf_paper_ids(session: AsyncSession, *, project_id: uuid.UUID) -> list[uuid.UUID]:
    """在架的全部 paper_id（前端标记「已入架」勾选态用；回收站里的不算）。"""
    stmt = (
        select(TopicPaper.paper_id)
        .where(TopicPaper.topic_id == project_id, TopicPaper.trashed_at.is_(None))
        .order_by(TopicPaper.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def _source_library_id(
    session: AsyncSession, *, project_id: uuid.UUID, paper_id: uuid.UUID
) -> uuid.UUID | None:
    """入架时的来源库溯源：本课题隐式库优先，其次任一含这篇论文的库；都没有 = 个人补充。"""
    stmt = (
        select(LibraryPaper.library_id, DirectionLibrary.project_id)
        .join(DirectionLibrary, DirectionLibrary.id == LibraryPaper.library_id)
        .where(LibraryPaper.paper_id == paper_id)
    )
    rows = list((await session.execute(stmt)).all())
    own = next((r for r in rows if r.project_id == project_id), None)
    source = own or (rows[0] if rows else None)
    return source.library_id if source is not None else None


async def add_to_shelf(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    user_id: uuid.UUID,
    note: str | None = None,
) -> dict[str, Any]:
    """入架：记来源库 + 同步 upsert 个人库（saved）。

    重复入架幂等：只更新 note。曾被移出（回收站里）的论文再次入架 = **复活那一行**
    ——唯一键 (topic_id, paper_id) 覆盖软删行，插新行会撞键；复活时清空回收站标记
    并按当下重取来源库，等同一次全新入架。
    """
    paper = await session.get(Paper, paper_id)
    if paper is None:
        raise PaperNotFoundError(str(paper_id))
    row = await _get_row(session, project_id=project_id, paper_id=paper_id, trashed=None)
    if row is not None and row.trashed_at is None:
        if note is not None:
            row.note = note
        await session.commit()
        return _item_dict(row, paper)

    source_library_id = await _source_library_id(
        session, project_id=project_id, paper_id=paper_id
    )
    if row is not None:  # 回收站里的旧行复活
        row.trashed_at = None
        row.trashed_by = None
        row.added_by = row.added_by or user_id
        # 来源库按当下重取；论文已不在任何库时保留旧溯源（比抹成空更有信息量）
        row.source_library_id = source_library_id or row.source_library_id
        if note is not None:
            row.note = note
    else:
        row = TopicPaper(
            topic_id=project_id,
            paper_id=paper_id,
            source_library_id=source_library_id,
            note=note,
            added_by=user_id,
        )
        session.add(row)
    await session.flush()
    # 入架必入个人库（书架是个人库的课题投影）；save_paper 内部 commit 一并落书架行
    await user_library.save_paper(session, user_id=user_id, paper=paper)
    return _item_dict(row, paper)


async def update_note(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    note: str | None,
) -> dict[str, Any]:
    row = await _get_row(session, project_id=project_id, paper_id=paper_id)
    if row is None:
        raise ShelfItemNotFoundError(str(paper_id))
    row.note = note
    await session.commit()
    paper = await session.get(Paper, paper_id)
    assert paper is not None  # 书架行外键保证
    return _item_dict(row, paper)


async def remove_from_shelf(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> None:
    """移出书架 = 软删（进课题回收站）；个人库条目与内容池行都不动。"""
    row = await _get_row(session, project_id=project_id, paper_id=paper_id)
    if row is None:
        raise ShelfItemNotFoundError(str(paper_id))
    row.trashed_at = utcnow()
    row.trashed_by = user_id
    await session.commit()


async def restore_from_shelf(
    session: AsyncSession, *, project_id: uuid.UUID, paper_id: uuid.UUID
) -> dict[str, Any]:
    """从回收站召回：清空回收站标记，条目原样回到书架（备注不动）。"""
    row = await _get_row(session, project_id=project_id, paper_id=paper_id, trashed=True)
    if row is None:
        raise ShelfItemNotFoundError(str(paper_id))
    row.trashed_at = None
    row.trashed_by = None
    await session.commit()
    paper = await session.get(Paper, paper_id)
    assert paper is not None  # 书架行外键保证
    return _item_dict(row, paper)


async def purge_from_shelf(
    session: AsyncSession, *, project_id: uuid.UUID, paper_id: uuid.UUID
) -> None:
    """彻底删除一条书架行（在架的也可一步删净）；个人库条目与内容池行都不动。"""
    row = await _get_row(session, project_id=project_id, paper_id=paper_id, trashed=None)
    if row is None:
        raise ShelfItemNotFoundError(str(paper_id))
    await session.delete(row)
    await session.commit()


async def empty_shelf_trash(session: AsyncSession, *, project_id: uuid.UUID) -> int:
    """清空课题回收站：彻底删除该课题全部软删书架行，返回删除条数。"""
    rows = (
        (
            await session.execute(
                select(TopicPaper).where(
                    TopicPaper.topic_id == project_id, TopicPaper.trashed_at.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        await session.delete(row)
    await session.commit()
    return len(rows)


class ShelfImportResult(NamedTuple):
    """个人补充入架结果：书架条目 + paper + 是否新建了内容池行。"""

    item: dict[str, Any]
    paper: Paper
    created: bool


async def import_to_shelf(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    arxiv_id: str | None = None,
    doi: str | None = None,
    title: str | None = None,
) -> ShelfImportResult:
    """个人补充入库：先按 dedup 查全局池，命中直接入架；未命中抓取解析入池后入架。

    查池 / 抓取入池那段与个人库手动添加共用（paper_import.resolve_or_create_pool_paper，
    不建任何 library_papers 成员行）；这里只负责把结果入架。解析失败抛
    paper_import.ParseFailedError（路由映射 422）。
    """
    result = await paper_import.resolve_or_create_pool_paper(
        session, arxiv_id=arxiv_id, doi=doi, title=title
    )
    item = await add_to_shelf(
        session, project_id=project_id, paper_id=result.paper.id, user_id=user_id
    )
    return ShelfImportResult(item=item, paper=result.paper, created=result.created)
