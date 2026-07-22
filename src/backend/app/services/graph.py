"""知识图谱构建（确定性代码，不走 LLM）：

节点：论文（打分通过及之后的状态）、概念（paper_concepts 关联）、作者（Paper.authors JSON）。
边：论文—概念（上链关系）、论文—作者（署名）。
规模控制：论文按相关度取 top ``max_papers``；作者按关联论文数取 top ``max_authors``，
单篇论文最多取前 ``authors_per_paper`` 位作者（长作者列表只保留头部署名）。
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import LibraryPaper
from app.models.paper import Concept, Paper, paper_concepts
from app.services.concepts import wiki_slug
from app.services.libraries import get_library_for_project

# 图上展示的论文状态（candidate 未过筛选，噪声大，不进图）
GRAPH_PAPER_STATUSES = ("scored", "fetched", "compiled", "included")

MAX_PAPERS = 150
MAX_AUTHORS = 80
AUTHORS_PER_PAPER = 8


def _author_names(raw: Any) -> list[str]:
    """兼容历史数据：[{"name": ...}] 或字符串列表。"""
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


async def project_graph(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    max_papers: int = MAX_PAPERS,
    max_authors: int = MAX_AUTHORS,
) -> dict[str, Any]:
    """构建项目知识图谱，返回 GraphResponse 形状的 dict。"""
    library = await get_library_for_project(session, project_id)
    paper_total = int(
        (
            await session.execute(
                select(func.count()).where(
                    LibraryPaper.library_id == library.id,
                    LibraryPaper.status.in_(GRAPH_PAPER_STATUSES),
                )
            )
        ).scalar_one()
    )
    rows = (
        await session.execute(
            select(Paper, LibraryPaper)
            .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
            .where(
                LibraryPaper.library_id == library.id,
                LibraryPaper.status.in_(GRAPH_PAPER_STATUSES),
            )
            .order_by(
                LibraryPaper.relevance_score.desc().nulls_last(),
                LibraryPaper.created_at.desc(),
            )
            .limit(max_papers)
        )
    ).all()
    papers = [p for p, _ in rows]
    membership_of = {p.id: m for p, m in rows}
    paper_ids = [p.id for p in papers]

    nodes: list[dict[str, Any]] = [
        {
            "id": str(p.id),
            "type": "paper",
            "label": p.title,
            "status": membership_of[p.id].status,
            "year": p.year,
            "published": p.published_at.date().isoformat() if p.published_at else None,
            "relevance": membership_of[p.id].relevance_score,
        }
        for p in papers
    ]
    edges: list[dict[str, str]] = []

    # —— 概念节点 + 论文—概念边 ——
    if paper_ids:
        rows = (
            await session.execute(
                select(paper_concepts.c.paper_id, Concept)
                .join(Concept, Concept.id == paper_concepts.c.concept_id)
                .where(paper_concepts.c.paper_id.in_(paper_ids))
            )
        ).all()
        concept_counts: dict[uuid.UUID, int] = {}
        concept_by_id: dict[uuid.UUID, Concept] = {}
        for paper_id, concept in rows:
            concept_by_id[concept.id] = concept
            concept_counts[concept.id] = concept_counts.get(concept.id, 0) + 1
            edges.append(
                {"source": str(paper_id), "target": str(concept.id), "kind": "paper_concept"}
            )
        nodes.extend(
            {
                "id": str(c.id),
                "type": "concept",
                "label": c.name,
                "category": c.category,
                "count": concept_counts[c.id],
            }
            for c in concept_by_id.values()
        )

    # —— 作者节点 + 论文—作者边（按关联论文数保留 top N 作者） ——
    author_papers: dict[str, list[uuid.UUID]] = {}
    author_label: dict[str, str] = {}
    for p in papers:
        for name in _author_names(p.authors)[:AUTHORS_PER_PAPER]:
            key = f"author:{wiki_slug(name)}"
            author_label.setdefault(key, name)
            author_papers.setdefault(key, []).append(p.id)
    kept_authors = sorted(author_papers, key=lambda k: len(author_papers[k]), reverse=True)[
        :max_authors
    ]
    for key in kept_authors:
        nodes.append(
            {
                "id": key,
                "type": "author",
                "label": author_label[key],
                "count": len(author_papers[key]),
            }
        )
        edges.extend(
            {"source": str(pid), "target": key, "kind": "paper_author"}
            for pid in author_papers[key]
        )

    return {
        "nodes": nodes,
        "edges": edges,
        "paper_total": paper_total,
        "truncated": paper_total > len(papers) or len(author_papers) > len(kept_authors),
    }
