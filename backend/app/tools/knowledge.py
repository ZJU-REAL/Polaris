"""知识底座只读工具：语义段落检索、论文详情、知识图谱、跨实体搜索。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.db import get_sessionmaker
from app.models.paper import Paper, PaperChunk
from app.services import chunks as chunks_service
from app.services import graph as graph_service
from app.services import search as search_service
from app.tools.context import ToolContext
from app.tools.registry import tool

_CHUNK_CHARS = 1200
_MAX_K = 12


@tool(
    "search_chunks",
    description="全文段落级语义检索（比 search_papers 更细，直接命中相关段落）",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题/关键词"},
            "k": {"type": "integer", "minimum": 1, "maximum": _MAX_K, "default": 6},
        },
        "required": ["query"],
    },
    summarize=lambda a, r: f"段落检索「{a.get('query', '')}」→ {len(r.get('chunks') or [])} 段",
)
async def search_chunks(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("search_chunks 需要非空 query")
    k = min(_MAX_K, max(1, int(args.get("k") or 6)))

    async with get_sessionmaker()() as session:
        rows: list[tuple[PaperChunk, float]] = []
        used_mode = "keyword"
        if chunks_service.chunk_vector_search_supported(session):
            try:
                vectors = await ctx.llm.embed(
                    [query],
                    user_id=ctx.user_id,
                    project_id=ctx.project_id,
                    voyage_id=ctx.voyage_id,
                )
                rows = await chunks_service.semantic_search_chunks(
                    session, project_id=ctx.project_id, query_vector=vectors[0], limit=k
                )
                used_mode = "semantic"
            except NotImplementedError:
                rows = []
        if not rows:
            rows = await chunks_service.keyword_search_chunks(
                session, project_id=ctx.project_id, q=query, limit=k
            )
            used_mode = used_mode if rows and used_mode == "semantic" else "keyword"

        # 补论文标题（一次批量查询，避免 N+1）
        paper_ids = {c.paper_id for c, _ in rows}
        titles: dict[uuid.UUID, str] = {}
        if paper_ids:
            title_rows = await session.execute(
                select(Paper.id, Paper.title).where(Paper.id.in_(paper_ids))
            )
            titles = {pid: title for pid, title in title_rows}

    return {
        "mode": used_mode,
        "chunks": [
            {
                "paper_id": str(c.paper_id),
                "title": titles.get(c.paper_id),
                "seq": c.seq,
                "text": (c.text or "")[:_CHUNK_CHARS],
                "score": round(float(score), 3),
            }
            for c, score in rows
        ],
    }


@tool(
    "get_paper",
    description="取某论文的元数据 + 概念标签（不含全文，全文用 read_fulltext）",
    input_schema={
        "type": "object",
        "properties": {"paper_id": {"type": "string", "description": "论文 uuid"}},
        "required": ["paper_id"],
    },
    summarize=lambda a, r: f"论文详情：{r.get('title', a.get('paper_id', ''))}",
)
async def get_paper(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        paper_id = uuid.UUID(str(args.get("paper_id")))
    except ValueError as e:
        raise ValueError(f"paper_id 不是合法 uuid：{args.get('paper_id')}") from e
    async with get_sessionmaker()() as session:
        stmt = (
            select(Paper)
            .where(Paper.id == paper_id, Paper.project_id == ctx.project_id)
            .options(selectinload(Paper.concepts))
        )
        paper = (await session.execute(stmt)).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"库内不存在该论文：{args.get('paper_id')}")
        authors = [a.get("name") for a in (paper.authors or []) if isinstance(a, dict)]
        return {
            "paper_id": str(paper.id),
            "title": paper.title,
            "year": paper.year,
            "venue": paper.venue,
            "authors": [a for a in authors if a][:20],
            "arxiv_id": paper.arxiv_id,
            "doi": paper.doi,
            "url": paper.url,
            "status": paper.status,
            "tldr": paper.tldr,
            "abstract": (paper.abstract or "")[:2000] or None,
            "concepts": [c.name for c in paper.concepts],
            "has_wiki": bool(paper.wiki_content),
            "has_fulltext": bool(paper.full_text_path),
        }


@tool(
    "knowledge_graph",
    description="项目知识图谱：论文/概念/作者节点与关联边",
    input_schema={"type": "object", "properties": {}},
    summarize=lambda a, r: f"知识图谱（{len(r.get('nodes') or [])} 节点）",
)
async def knowledge_graph(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        return await graph_service.project_graph(session, project_id=ctx.project_id)


@tool(
    "global_search",
    description="跨实体检索：论文/概念/想法/实验/稿件/任务（确定性 ilike）",
    input_schema={
        "type": "object",
        "properties": {"q": {"type": "string", "description": "检索关键词"}},
        "required": ["q"],
    },
    summarize=lambda a, r: f"全局检索「{a.get('q', '')}」→ {len(r.get('hits') or [])} 条",
)
async def global_search(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    q = str(args.get("q") or "").strip()
    if not q:
        raise ValueError("global_search 需要非空 q")
    async with get_sessionmaker()() as session:
        hits = await search_service.global_search(session, project_id=ctx.project_id, q=q)
    return {"hits": [h.model_dump(mode="json") for h in hits]}
