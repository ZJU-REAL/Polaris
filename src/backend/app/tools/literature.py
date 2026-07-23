"""文献 / 概念只读检索工具（原 agents/voyage/lit_tools.py 平移到统一注册表）。

工具全部是确定性代码（复用既有 services），只读、按项目隔离。
结果为可直接注入 prompt 的紧凑 JSON。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper import Concept, Paper
from app.services import concepts as concepts_service
from app.services import papers as papers_service
from app.services.libraries import get_source_library_ids, membership_for_project
from app.services.paper_review import relevant_excerpt
from app.services.papers import PaperView
from app.tools.context import ToolContext
from app.tools.registry import tool

_WIKI_CHARS = 8000
_FULLTEXT_PAGE_CHARS = 6000
_EXCERPT_CHARS = 2400
_MAX_K = 10

_CONCEPT_CATEGORIES = [
    "method",
    "architecture",
    "methodology",
    "problem",
    "metric",
    "dataset",
    "other",
]


def _paper_brief(paper: PaperView, score: float | None = None) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "paper_id": str(paper.id),
        "title": paper.title,
        "year": paper.year,
        "tldr": (paper.tldr or (paper.abstract or "")[:200]) or None,
        "has_wiki": bool(paper.wiki_content),
        "has_fulltext": bool(paper.full_text_path),
    }
    if score is not None:
        brief["score"] = round(float(score), 3)
    return brief


async def _get_project_paper(session: Any, ctx: ToolContext, raw_id: Any) -> PaperView:
    try:
        paper_id = uuid.UUID(str(raw_id))
    except ValueError as e:
        raise ValueError(f"paper_id 不是合法 uuid：{raw_id}") from e
    paper = await session.get(Paper, paper_id)
    membership = (
        await membership_for_project(session, project_id=ctx.project_id, paper_id=paper_id)
        if paper is not None
        else None
    )
    if paper is None or membership is None:
        raise ValueError(f"库内不存在该论文：{raw_id}")
    return PaperView(paper, membership, ctx.project_id)


@tool(
    "search_papers",
    description="库内检索论文",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词"},
            "mode": {"type": "string", "enum": ["keyword", "semantic"], "default": "semantic"},
            "k": {"type": "integer", "minimum": 1, "maximum": _MAX_K, "default": 5},
        },
        "required": ["query"],
    },
    summarize=lambda a, r: f"检索「{a.get('query', '')}」→ {len(r.get('results') or [])} 篇",
)
async def search_papers(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("search_papers 需要非空 query")
    k = min(_MAX_K, max(1, int(args.get("k") or 5)))
    mode = str(args.get("mode") or "semantic")

    async with get_sessionmaker()() as session:
        rows: list[tuple[Paper, float]] = []
        used_mode = "keyword"
        if mode == "semantic" and papers_service.semantic_search_supported(session):
            try:
                vectors = await ctx.llm.embed(
                    [query],
                    user_id=ctx.user_id,
                    project_id=ctx.project_id,
                    voyage_id=ctx.voyage_id,
                )
                rows = await papers_service.semantic_search_papers(
                    session, project_id=ctx.project_id, query_vector=vectors[0], limit=k
                )
                used_mode = "semantic"
            except NotImplementedError:
                rows = []
        if not rows:  # semantic 不可用/无召回 → 关键词降级
            rows = await papers_service.keyword_search_papers(
                session, project_id=ctx.project_id, q=query, limit=k
            )
            used_mode = used_mode if rows and used_mode == "semantic" else "keyword"
        return {
            "mode": used_mode,
            "results": [_paper_brief(p, score) for p, score in rows],
        }


@tool(
    "read_wiki",
    description="读某论文的 wiki 综述页",
    input_schema={
        "type": "object",
        "properties": {"paper_id": {"type": "string", "description": "论文 uuid"}},
        "required": ["paper_id"],
    },
    summarize=lambda a, r: f"阅读 wiki：{r.get('title', a.get('paper_id', ''))}",
)
async def read_wiki(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        paper = await _get_project_paper(session, ctx, args.get("paper_id"))
        content = paper.wiki_content or ""
        if not content:
            return {
                "paper_id": str(paper.id),
                "title": paper.title,
                "wiki": None,
                "abstract": (paper.abstract or "")[:2000] or None,
                "note": "该论文尚未编译 wiki 页，返回摘要",
            }
        return {
            "paper_id": str(paper.id),
            "title": paper.title,
            "wiki": content[:_WIKI_CHARS],
            "truncated": len(content) > _WIKI_CHARS,
        }


def _read_fulltext_summary(a: dict[str, Any], r: dict[str, Any]) -> str:
    target = r.get("title", a.get("paper_id", ""))
    return f"查阅全文：{target}" + (f"（定位「{a['query']}」）" if a.get("query") else "")


@tool(
    "read_fulltext",
    description="读论文全文（有 query 返回最相关段落，否则按 page 分页）",
    input_schema={
        "type": "object",
        "properties": {
            "paper_id": {"type": "string", "description": "论文 uuid"},
            "query": {"type": "string", "description": "定位关键词（可选）"},
            "page": {"type": "integer", "minimum": 0, "default": 0},
        },
        "required": ["paper_id"],
    },
    summarize=_read_fulltext_summary,
)
async def read_fulltext(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        paper = await _get_project_paper(session, ctx, args.get("paper_id"))
        path = Path(paper.full_text_path) if paper.full_text_path else None
        title = paper.title
        paper_id = str(paper.id)
        if path is None or not path.is_file():
            return {
                "paper_id": paper_id,
                "title": title,
                "text": None,
                "note": "该论文无全文，可改用 read_wiki 或摘要",
                "abstract": (paper.abstract or "")[:2000] or None,
            }
        full_text = path.read_text(encoding="utf-8", errors="ignore")

    query = str(args.get("query") or "").strip()
    if query:
        return {
            "paper_id": paper_id,
            "title": title,
            "query": query,
            "text": relevant_excerpt(full_text, query, max_chars=_EXCERPT_CHARS),
        }
    pages = max(1, (len(full_text) + _FULLTEXT_PAGE_CHARS - 1) // _FULLTEXT_PAGE_CHARS)
    page = min(pages - 1, max(0, int(args.get("page") or 0)))
    start = page * _FULLTEXT_PAGE_CHARS
    return {
        "paper_id": paper_id,
        "title": title,
        "page": page,
        "pages": pages,
        "text": full_text[start : start + _FULLTEXT_PAGE_CHARS],
    }


@tool(
    "get_concept",
    description="概念定义 + 相关概念 + 关联论文",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "概念名"}},
        "required": ["name"],
    },
    summarize=lambda a, r: f"查看概念「{a.get('name', '')}」",
)
async def get_concept(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    name = str(args.get("name") or "").strip()
    if not name:
        raise ValueError("get_concept 需要非空 name")
    async with get_sessionmaker()() as session:
        library_ids = await get_source_library_ids(session, ctx.project_id)
        if not library_ids:
            return {"name": name, "found": False, "note": "概念库中没有该概念"}
        stmt = select(Concept).where(
            Concept.library_id.in_(library_ids), Concept.name.ilike(name)
        )
        concept = (await session.execute(stmt)).scalars().first()
        if concept is None:  # 精确不中再模糊
            stmt = (
                select(Concept)
                .where(Concept.library_id.in_(library_ids), Concept.name.ilike(f"%{name}%"))
                .order_by(Concept.name)
            )
            concept = (await session.execute(stmt)).scalars().first()
        if concept is None:
            return {"name": name, "found": False, "note": "概念库中没有该概念"}
        related = await concepts_service.related_concepts(session, concept)
        papers = await concepts_service.papers_of_concept(session, concept.id)
        return {
            "name": concept.name,
            "found": True,
            "category": concept.category,
            "definition": concept.definition,
            "related": [{"name": c.name, "cooccur": n} for c, n in related],
            "papers": [
                {"paper_id": str(p.id), "title": p.title, "year": p.year} for p in papers[:10]
            ],
        }


@tool(
    "list_concepts",
    description="项目概念清单",
    input_schema={
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": _CONCEPT_CATEGORIES, "description": "可选"},
        },
    },
    summarize=lambda a, r: f"浏览概念清单（{len(r.get('concepts') or [])} 个）",
)
async def list_concepts(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    category = str(args.get("category") or "").strip() or None
    async with get_sessionmaker()() as session:
        library_ids = await get_source_library_ids(session, ctx.project_id)
        rows = await concepts_service.list_concepts(
            session, library_ids=library_ids, category=category
        )
    return {
        "concepts": [
            {"name": c.name, "category": c.category, "paper_count": n} for c, n in rows[:100]
        ]
    }
