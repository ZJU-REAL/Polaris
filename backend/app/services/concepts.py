"""概念库业务逻辑：wikilink 解析、slug、列表/详情查询（不 import fastapi）。"""

import hashlib
import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Concept, Paper, paper_concepts

# [[概念名]] / [[概念名|别名]] / [[概念名#锚点]]
WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+?)(?:[|#][^\[\]]*)?\]\]")

CONCEPT_CATEGORIES = (
    "method",
    "architecture",
    "methodology",
    "problem",
    "metric",
    "dataset",
    "other",
)

_SLUG_STRIP_RE = re.compile(r"[^\w一-鿿]+", re.UNICODE)


def extract_wikilinks(markdown: str) -> list[str]:
    """解析 [[..]] 双链，返回去重（保序）的概念名列表。"""
    seen: dict[str, None] = {}
    for match in WIKILINK_RE.finditer(markdown or ""):
        name = match.group(1).strip()
        if name:
            seen.setdefault(name, None)
    return list(seen)


def wiki_slug(name: str) -> str:
    """概念/论文 slug：保留中英文字符，其余折叠为 '-'；空则退回内容 hash。"""
    slug = _SLUG_STRIP_RE.sub("-", name.strip().lower()).strip("-")
    return slug or hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def normalize_category(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in CONCEPT_CATEGORIES else "other"


async def list_concepts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    category: str | None = None,
    q: str | None = None,
) -> list[tuple[Concept, int]]:
    """项目概念列表（附 paper_count）。"""
    paper_count = func.count(paper_concepts.c.paper_id).label("paper_count")
    stmt = (
        select(Concept, paper_count)
        .outerjoin(paper_concepts, paper_concepts.c.concept_id == Concept.id)
        .where(Concept.project_id == project_id)
        .group_by(Concept.id)
        .order_by(paper_count.desc(), Concept.name)
    )
    if category:
        stmt = stmt.where(Concept.category == category)
    if q:
        stmt = stmt.where(Concept.name.ilike(f"%{q}%"))
    return [(concept, int(count)) for concept, count in (await session.execute(stmt)).all()]


async def paper_count_of(session: AsyncSession, concept_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(paper_concepts.c.concept_id == concept_id)
    return int((await session.execute(stmt)).scalar_one())


async def papers_of_concept(session: AsyncSession, concept_id: uuid.UUID) -> list[Paper]:
    stmt = (
        select(Paper)
        .join(paper_concepts, paper_concepts.c.paper_id == Paper.id)
        .where(paper_concepts.c.concept_id == concept_id)
        .order_by(Paper.published_at.desc().nulls_last(), Paper.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def related_concepts(
    session: AsyncSession, concept: Concept, limit: int = 10
) -> list[tuple[Concept, int]]:
    """相关概念 = 与本概念共现于同一论文的概念，按共现次数取 top N。"""
    pc_self = paper_concepts.alias("pc_self")
    pc_other = paper_concepts.alias("pc_other")
    cooccur = func.count().label("cooccur")
    stmt = (
        select(Concept, cooccur)
        .join(pc_other, pc_other.c.concept_id == Concept.id)
        .join(pc_self, pc_self.c.paper_id == pc_other.c.paper_id)
        .where(pc_self.c.concept_id == concept.id, Concept.id != concept.id)
        .group_by(Concept.id)
        .order_by(cooccur.desc(), Concept.name)
        .limit(limit)
    )
    return [(c, int(n)) for c, n in (await session.execute(stmt)).all()]
