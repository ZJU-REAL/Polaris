"""论文库与检索业务逻辑（不 import fastapi）。"""

import asyncio
import json
import logging
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
from app.models.paper import Concept, Paper, PaperNote, PaperTag, PaperUserMeta, paper_tag_links
from app.models.project import ProjectMember

logger = logging.getLogger(__name__)

PAPER_SORTS = ("relevance", "-published_at")

# 语义检索重排：向量召回候选数 / 送重排的文档截断长度
RERANK_CANDIDATES = 30
RERANK_DOC_CHARS = 512

# status 组别名：库内（相关性达标及之后）/ 待编译（达标但未编译）/ 已编译（含人工纳入的历史数据）
PAPER_STATUS_GROUPS: dict[str, tuple[str, ...]] = {
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


def _member_paper_filter(stmt: Select, user_id: uuid.UUID) -> Select:
    return stmt.join(ProjectMember, ProjectMember.project_id == Paper.project_id).where(
        ProjectMember.user_id == user_id
    )


def apply_paper_filters(
    stmt: Select,
    *,
    project_id: uuid.UUID,
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
    """论文列表 / 引用导出共用的过滤条件（starred / reading_status 为 user_id 的个人视角）。

    status 支持组别名（docs/api-lit.md §8.5）：
    - ``library``：库内文献 = 相关性达标及之后的状态（scored/fetched/compiled/included）
    - ``pending_compile``：待编译 = 已达标但还没有解读（scored/fetched）
    """
    stmt = stmt.where(Paper.project_id == project_id)
    if status in PAPER_STATUS_GROUPS:
        stmt = stmt.where(Paper.status.in_(PAPER_STATUS_GROUPS[status]))
    elif status:
        stmt = stmt.where(Paper.status == status)
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
        stmt = stmt.where(Paper.created_at >= created_from)
    if created_to:
        stmt = stmt.where(Paper.created_at <= created_to)
    if tag:
        stmt = stmt.where(
            Paper.id.in_(
                select(paper_tag_links.c.paper_id)
                .join(PaperTag, PaperTag.id == paper_tag_links.c.tag_id)
                .where(PaperTag.project_id == project_id, PaperTag.name == tag)
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
    project_id: uuid.UUID,
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
) -> tuple[Sequence[Paper], int]:
    stmt = apply_paper_filters(
        select(Paper),
        project_id=project_id,
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
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    if sort == "-published_at":
        stmt = stmt.order_by(Paper.published_at.desc().nulls_last(), Paper.created_at.desc())
    else:  # relevance（默认）
        stmt = stmt.order_by(Paper.relevance_score.desc().nulls_last(), Paper.created_at.desc())
    stmt = stmt.offset((page - 1) * size).limit(size)
    return (await session.execute(stmt)).scalars().all(), int(total)


async def get_paper_for_user(
    session: AsyncSession, *, paper_id: uuid.UUID, user_id: uuid.UUID, with_concepts: bool = False
) -> Paper | None:
    """取论文；非项目成员视为不存在。"""
    stmt = _member_paper_filter(select(Paper), user_id).where(Paper.id == paper_id)
    if with_concepts:
        stmt = stmt.options(selectinload(Paper.concepts))
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_paper_status(session: AsyncSession, paper: Paper, status: str) -> Paper:
    paper.status = status
    await session.commit()
    await session.refresh(paper)
    return paper


# ---- 标签 / 个人状态 / 笔记数聚合（docs/api-lit.md §5） ----


async def paper_extras_map(
    session: AsyncSession, *, paper_ids: Sequence[uuid.UUID], user_id: uuid.UUID
) -> dict[uuid.UUID, dict[str, Any]]:
    """批量取论文的 tags / starred / reading_status / note_count（3 条聚合查询，避免 N+1）。"""
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
        .where(PaperNote.paper_id.in_(ids))
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


async def set_paper_tags(session: AsyncSession, paper: Paper, names: list[str]) -> list[str]:
    """整组覆盖论文标签：新名字自动建 tag，空数组=清空。返回排序后的标签名。"""
    cleaned = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    existing = (
        (
            await session.execute(
                select(PaperTag).where(
                    PaperTag.project_id == paper.project_id, PaperTag.name.in_(cleaned or [""])
                )
            )
        )
        .scalars()
        .all()
    )
    by_name = {t.name: t for t in existing}
    for name in cleaned:
        if name not in by_name:
            tag = PaperTag(project_id=paper.project_id, name=name)
            session.add(tag)
            by_name[name] = tag
    await session.flush()
    await session.execute(delete(paper_tag_links).where(paper_tag_links.c.paper_id == paper.id))
    if cleaned:
        await session.execute(
            insert(paper_tag_links).values(
                [{"paper_id": paper.id, "tag_id": by_name[n].id} for n in cleaned]
            )
        )
    await session.commit()
    return sorted(cleaned)


async def list_project_tags(
    session: AsyncSession, *, project_id: uuid.UUID
) -> list[dict[str, Any]]:
    """项目标签列表（含引用论文数），按名称排序。"""
    rows = await session.execute(
        select(PaperTag.id, PaperTag.name, func.count(paper_tag_links.c.paper_id))
        .outerjoin(paper_tag_links, paper_tag_links.c.tag_id == PaperTag.id)
        .where(PaperTag.project_id == project_id)
        .group_by(PaperTag.id, PaperTag.name)
        .order_by(PaperTag.name)
    )
    return [{"id": tid, "name": name, "paper_count": int(count)} for tid, name, count in rows]


async def upsert_paper_user_meta(
    session: AsyncSession,
    *,
    paper: Paper,
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


# ---- 删除论文（docs/api-lit.md §8.6） ----


def _remove_paper_files(paper: Paper) -> None:
    """尽力清理论文落盘文件（PDF / 全文 / 图片目录）；失败只记日志。"""
    import shutil

    from app.services.literature.pdf_extract import figures_dir

    for raw in (paper.pdf_path, paper.full_text_path):
        if not raw:
            continue
        try:
            Path(raw).unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to remove paper file %s", raw, exc_info=True)
    try:
        fig_dir = figures_dir(str(paper.id)).parent  # <data_dir>/papers/<paper_id>/
        if fig_dir.exists():
            shutil.rmtree(fig_dir, ignore_errors=True)
    except OSError:
        logger.warning("failed to remove figures dir for %s", paper.id, exc_info=True)


async def delete_paper(session: AsyncSession, paper: Paper) -> None:
    """删除一篇论文：清理落盘文件 + 删行（分段/笔记/标签/概念关联 FK 级联）。"""
    _remove_paper_files(paper)
    await session.delete(paper)
    await session.commit()


async def delete_papers(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    paper_ids: list[uuid.UUID],
    hard: bool = False,
) -> int:
    """批量删除项目内论文（非本项目的 id 忽略），返回处理数。

    默认软删（移入垃圾桶 = status excluded，可召回）；hard=True 彻底删除（清文件+删行）。
    """
    papers = (
        (
            await session.execute(
                select(Paper).where(Paper.project_id == project_id, Paper.id.in_(paper_ids))
            )
        )
        .scalars()
        .all()
    )
    for paper in papers:
        if hard:
            _remove_paper_files(paper)
            await session.delete(paper)
        else:
            paper.status = "excluded"
    await session.commit()
    return len(papers)


def restore_status_of(paper: Paper) -> str:
    """垃圾桶召回后的状态：已编译回 compiled；打过分回 scored；否则按人工精选处理。"""
    if paper.wiki_content:
        return "compiled"
    if paper.relevance_score is not None:
        return "scored"
    return "included"


async def restore_paper(session: AsyncSession, paper: Paper) -> Paper:
    """从垃圾桶召回（docs/api-lit.md §8.6）。"""
    paper.status = restore_status_of(paper)
    await session.commit()
    await session.refresh(paper)
    return paper


async def empty_trash(session: AsyncSession, *, project_id: uuid.UUID) -> int:
    """清空垃圾桶：彻底删除项目内全部 excluded 论文（清文件 + 删行），返回删除数。"""
    papers = (
        (
            await session.execute(
                select(Paper).where(Paper.project_id == project_id, Paper.status == "excluded")
            )
        )
        .scalars()
        .all()
    )
    for paper in papers:
        _remove_paper_files(paper)
        await session.delete(paper)
    await session.commit()
    return len(papers)


# ---- PDF 按需补下（docs/api-lit.md §1） ----


async def fetch_pdf(session: AsyncSession, paper: Paper) -> Paper:
    """按需补下 PDF + 抽全文；已有 PDF 文件时幂等直接返回。

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
    await session.commit()
    await session.refresh(paper)
    return paper


# ---- AI 伴读上下文（docs/api-lit.md §3） ----


def build_chat_context(paper: Paper) -> str:
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
- 只依据下面给出的论文内容回答，不要编造论文里没有的信息；
- 论文内容里没有提到或你不确定的，直接说明「论文中未提及」或「不确定」；
- 用中文回答，讲清楚、说人话。

论文标题：{title}

论文内容：
{context}
"""


def build_chat_messages(
    paper: Paper, *, question: str, history: Sequence[tuple[str, str]] = ()
) -> list[Message]:
    """组装伴读消息：system（论文上下文）+ 历史对话（前端携带）+ 当前问题。"""
    messages = [
        Message(
            role="system",
            content=CHAT_SYSTEM_PROMPT_TEMPLATE.format(
                title=paper.title, context=build_chat_context(paper)
            ),
        )
    ]
    messages += [Message(role=role, content=content) for role, content in history]
    messages.append(Message(role="user", content=question))
    return messages


# ---- 检索 ----


async def keyword_search_papers(
    session: AsyncSession, *, project_id: uuid.UUID, q: str, limit: int
) -> list[tuple[Paper, float]]:
    """关键词检索：title/abstract/wiki_content/笔记内容 ilike，按命中位置给启发式分。

    只检索库内文献（相关性达标）：已删除（excluded）/未筛选（candidate）不出现。
    """
    pattern = f"%{q}%"
    note_hit = Paper.id.in_(
        select(PaperNote.paper_id).where(
            PaperNote.project_id == project_id, PaperNote.content.ilike(pattern)
        )
    )
    stmt = (
        select(Paper)
        .where(
            Paper.status.in_(PAPER_STATUS_GROUPS["library"]),
            Paper.project_id == project_id,
            or_(
                Paper.title.ilike(pattern),
                Paper.abstract.ilike(pattern),
                Paper.wiki_content.ilike(pattern),
                note_hit,
            ),
        )
        .limit(limit * 3)
    )
    papers = (await session.execute(stmt)).scalars().all()
    needle = q.lower()

    def score_of(p: Paper) -> float:
        if needle in (p.title or "").lower():
            return 1.0
        if needle in (p.abstract or "").lower():
            return 0.7
        return 0.5  # wiki_content / 笔记命中

    ranked = sorted(((p, score_of(p)) for p in papers), key=lambda x: -x[1])
    return ranked[:limit]


async def keyword_search_concepts(
    session: AsyncSession, *, project_id: uuid.UUID, q: str, limit: int
) -> list[tuple[Concept, float]]:
    stmt = (
        select(Concept)
        .where(Concept.project_id == project_id, Concept.name.ilike(f"%{q}%"))
        .order_by(Concept.name)
        .limit(limit)
    )
    return [(c, 1.0) for c in (await session.execute(stmt)).scalars().all()]


def semantic_search_supported(session: AsyncSession) -> bool:
    return session.get_bind().dialect.name == "postgresql"


async def semantic_search_papers(
    session: AsyncSession, *, project_id: uuid.UUID, query_vector: list[float], limit: int
) -> list[tuple[Paper, float]]:
    """pgvector 余弦检索（仅 postgres；调用方需先判 semantic_search_supported）。"""
    qv = json.dumps(query_vector)
    rows = (
        await session.execute(
            text(
                "SELECT id, 1 - (embedding <=> CAST(:qv AS vector)) AS score "
                "FROM papers "
                "WHERE project_id = :pid AND embedding IS NOT NULL "
                "ORDER BY embedding <=> CAST(:qv AS vector) "
                "LIMIT :k"
            ),
            {"qv": qv, "pid": str(project_id), "k": limit},
        )
    ).all()
    if not rows:
        return []
    scores = {row.id: float(row.score) for row in rows}
    papers = (
        (await session.execute(select(Paper).where(Paper.id.in_(list(scores))))).scalars().all()
    )
    by_id = {p.id: p for p in papers}
    return [(by_id[pid], scores[pid]) for pid, _ in ((r.id, r.score) for r in rows) if pid in by_id]


def rerank_document_of(paper: Paper) -> str:
    """重排送审文本：title + abstract，截断 RERANK_DOC_CHARS 字。"""
    text_ = paper.title or ""
    if paper.abstract:
        text_ = f"{text_}\n{paper.abstract}"
    return text_[:RERANK_DOC_CHARS]


async def rerank_paper_rows(
    llm_router: LLMRouter,
    *,
    query: str,
    rows: list[tuple[Paper, float]],
    limit: int,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> tuple[list[tuple[Paper, float]], bool]:
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
