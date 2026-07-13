"""论文库与检索业务逻辑（不 import fastapi）。"""

import json
import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.paper import Concept, Paper
from app.models.project import ProjectMember

PAPER_SORTS = ("relevance", "-published_at")


def _member_paper_filter(stmt: Select, user_id: uuid.UUID) -> Select:
    return stmt.join(ProjectMember, ProjectMember.project_id == Paper.project_id).where(
        ProjectMember.user_id == user_id
    )


async def list_papers(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    status: str | None = None,
    q: str | None = None,
    sort: str = "relevance",
    page: int = 1,
    size: int = 20,
) -> tuple[Sequence[Paper], int]:
    stmt = select(Paper).where(Paper.project_id == project_id)
    if status:
        stmt = stmt.where(Paper.status == status)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(Paper.title.ilike(pattern), Paper.abstract.ilike(pattern)))
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


# ---- 检索 ----


async def keyword_search_papers(
    session: AsyncSession, *, project_id: uuid.UUID, q: str, limit: int
) -> list[tuple[Paper, float]]:
    """关键词检索：title/abstract/wiki_content ilike，按命中位置给启发式分。"""
    pattern = f"%{q}%"
    stmt = (
        select(Paper)
        .where(
            Paper.project_id == project_id,
            or_(
                Paper.title.ilike(pattern),
                Paper.abstract.ilike(pattern),
                Paper.wiki_content.ilike(pattern),
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
        return 0.5  # wiki_content 命中

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
