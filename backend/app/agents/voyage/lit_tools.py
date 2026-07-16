"""文献工具集：目标构建 / 方案深耕的确定性检索工具（docs/api-idea2.md §3）。

工具全部是普通代码（复用既有 services），LLM 只负责决定调用哪个工具、带什么参数；
结果为可直接注入 prompt 的紧凑 JSON。未知工具/非法参数抛 ValueError，
由工具循环捕获并作为错误消息回给 LLM（不打断循环）。
"""

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.agents.voyage.actions import ActionContext
from app.core.db import get_sessionmaker
from app.models.paper import Concept, Paper
from app.services import concepts as concepts_service
from app.services import papers as papers_service
from app.services.paper_review import relevant_excerpt

_WIKI_CHARS = 8000
_FULLTEXT_PAGE_CHARS = 6000
_EXCERPT_CHARS = 2400
_MAX_K = 10

TOOL_SPECS = """\
- search_papers {"query": "关键词", "mode": "keyword"|"semantic", "k": 5}：库内检索论文
- read_wiki {"paper_id": "uuid"}：读某论文的 wiki 综述页
- read_fulltext {"paper_id": "uuid", "query": "定位关键词（可选）", "page": 0}：\
  读论文全文（有 query 返回最相关段落，否则按 page 分页）
- get_concept {"name": "概念名"}：概念定义 + 相关概念 + 关联论文
- list_concepts {"category": "method|architecture|methodology|problem|metric|dataset|other\
  （可选）"}：项目概念清单"""


def _paper_brief(paper: Paper, score: float | None = None) -> dict[str, Any]:
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


async def _get_project_paper(session: Any, ctx: ActionContext, raw_id: Any) -> Paper:
    try:
        paper_id = uuid.UUID(str(raw_id))
    except ValueError as e:
        raise ValueError(f"paper_id 不是合法 uuid：{raw_id}") from e
    paper = await session.get(Paper, paper_id)
    if paper is None or paper.project_id != ctx.run.project_id:
        raise ValueError(f"库内不存在该论文：{raw_id}")
    return paper


async def _search_papers(ctx: ActionContext, args: dict[str, Any]) -> dict[str, Any]:
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
                    user_id=ctx.run.created_by,
                    project_id=ctx.run.project_id,
                    voyage_id=ctx.run.id,
                )
                rows = await papers_service.semantic_search_papers(
                    session, project_id=ctx.run.project_id, query_vector=vectors[0], limit=k
                )
                used_mode = "semantic"
            except NotImplementedError:
                rows = []
        if not rows:  # semantic 不可用/无召回 → 关键词降级
            rows = await papers_service.keyword_search_papers(
                session, project_id=ctx.run.project_id, q=query, limit=k
            )
            used_mode = used_mode if rows and used_mode == "semantic" else "keyword"
        return {
            "mode": used_mode,
            "results": [_paper_brief(p, score) for p, score in rows],
        }


async def _read_wiki(ctx: ActionContext, args: dict[str, Any]) -> dict[str, Any]:
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


async def _read_fulltext(ctx: ActionContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        paper = await _get_project_paper(session, ctx, args.get("paper_id"))
        path = Path(paper.full_text_path) if paper.full_text_path else None
        if path is None or not path.is_file():
            return {
                "paper_id": str(paper.id),
                "title": paper.title,
                "text": None,
                "note": "该论文无全文，可改用 read_wiki 或摘要",
                "abstract": (paper.abstract or "")[:2000] or None,
            }
        full_text = path.read_text(encoding="utf-8", errors="ignore")

    query = str(args.get("query") or "").strip()
    if query:
        return {
            "paper_id": str(paper.id),
            "title": paper.title,
            "query": query,
            "text": relevant_excerpt(full_text, query, max_chars=_EXCERPT_CHARS),
        }
    pages = max(1, (len(full_text) + _FULLTEXT_PAGE_CHARS - 1) // _FULLTEXT_PAGE_CHARS)
    page = min(pages - 1, max(0, int(args.get("page") or 0)))
    start = page * _FULLTEXT_PAGE_CHARS
    return {
        "paper_id": str(paper.id),
        "title": paper.title,
        "page": page,
        "pages": pages,
        "text": full_text[start : start + _FULLTEXT_PAGE_CHARS],
    }


async def _get_concept(ctx: ActionContext, args: dict[str, Any]) -> dict[str, Any]:
    name = str(args.get("name") or "").strip()
    if not name:
        raise ValueError("get_concept 需要非空 name")
    async with get_sessionmaker()() as session:
        stmt = select(Concept).where(
            Concept.project_id == ctx.run.project_id, Concept.name.ilike(name)
        )
        concept = (await session.execute(stmt)).scalars().first()
        if concept is None:  # 精确不中再模糊
            stmt = (
                select(Concept)
                .where(Concept.project_id == ctx.run.project_id, Concept.name.ilike(f"%{name}%"))
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


async def _list_concepts(ctx: ActionContext, args: dict[str, Any]) -> dict[str, Any]:
    category = str(args.get("category") or "").strip() or None
    async with get_sessionmaker()() as session:
        rows = await concepts_service.list_concepts(
            session, project_id=ctx.run.project_id, category=category
        )
    return {
        "concepts": [
            {"name": c.name, "category": c.category, "paper_count": n} for c, n in rows[:100]
        ]
    }


_TOOLS = {
    "search_papers": _search_papers,
    "read_wiki": _read_wiki,
    "read_fulltext": _read_fulltext,
    "get_concept": _get_concept,
    "list_concepts": _list_concepts,
}


def known_tools() -> frozenset[str]:
    return frozenset(_TOOLS)


async def run_tool(ctx: ActionContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """执行单个工具；未知工具/非法参数抛 ValueError（调用方转为错误消息回给 LLM）。"""
    tool = _TOOLS.get(name)
    if tool is None:
        raise ValueError(f"未知工具：{name}（可用：{', '.join(sorted(_TOOLS))}）")
    if not isinstance(args, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    return await tool(ctx, args)
