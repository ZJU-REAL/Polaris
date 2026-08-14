"""知识底座只读工具：语义段落检索、论文详情、知识图谱、跨实体搜索。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper import Paper, PaperChunk
from app.services import chunks as chunks_service
from app.services import graph as graph_service
from app.services import search as search_service
from app.services.embedding import embed_query
from app.tools.context import ToolContext
from app.tools.registry import tool
from app.tools.scope import library_ids_for, readable_paper

_CHUNK_CHARS = 1200
_MAX_K = 12


@tool(
    "search_chunks",
    description="在本课题语料内做段落级检索，直接返回相关段落，粒度比 search_papers 更细",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题/关键词"},
            "mode": {"type": "string", "enum": ["keyword", "semantic"], "default": "semantic"},
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
    mode = str(args.get("mode") or "semantic")

    async with get_sessionmaker()() as session:
        library_ids = await library_ids_for(session, ctx)
        rows: list[tuple[PaperChunk, float]] = []
        used_mode = "keyword"
        if (
            mode == "semantic"
            and library_ids
            and chunks_service.chunk_vector_search_supported(session)
        ):
            try:
                vector, space = await embed_query(
                    session,
                    query,
                    user_id=ctx.user_id,
                    project_id=ctx.project_id,
                    voyage_id=ctx.voyage_id,
                )
                rows = await chunks_service.semantic_search_chunks(
                    session,
                    library_ids=library_ids,
                    query_vector=vector,
                    space=space,
                    limit=k,
                )
                used_mode = "semantic"
            except Exception:  # noqa: BLE001 — embedding 服务挂了也要能用，降级到关键词
                rows = []
        if not rows and library_ids:
            rows = await chunks_service.keyword_search_chunks(
                session, library_ids=library_ids, q=query, limit=k
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
    description="取某篇论文的元数据与概念标签，不含全文；全文请用 read_fulltext",
    input_schema={
        "type": "object",
        "properties": {"paper_id": {"type": "string", "description": "论文 uuid"}},
        "required": ["paper_id"],
    },
    summarize=lambda a, r: f"论文详情：{r.get('title', a.get('paper_id', ''))}",
)
async def get_paper(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        access = await readable_paper(session, ctx, args.get("paper_id"), with_concepts=True)
        paper = access.view
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
            # 没进库的论文（每日新论文/书架/个人库）也读得到，但别让模型把它当成
            # 「你库里的工作」——status 在这种情况下是合成的，说明不了收录与否。
            "in_library": access.in_library,
            "tldr": paper.tldr,
            "abstract": (paper.abstract or "")[:2000] or None,
            "concepts": [c.name for c in paper.concepts],
            "has_wiki": paper.has_wiki,
            "has_fulltext": bool(paper.full_text_path),
        }


@tool(
    "knowledge_graph",
    description="取本课题的知识图谱，含论文、概念、作者节点及其关联边",
    input_schema={"type": "object", "properties": {}},
    summarize=lambda a, r: f"知识图谱（{len(r.get('nodes') or [])} 节点）",
)
async def knowledge_graph(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.project_id is None:
        raise ValueError("knowledge_graph 需要指定课题：知识图谱是按课题组织的")
    async with get_sessionmaker()() as session:
        return await graph_service.project_graph(session, project_id=ctx.project_id)


@tool(
    "global_search",
    description="在本课题内按关键词跨实体检索论文、概念、想法、实验、稿件与任务",
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
    if ctx.project_id is None:
        raise ValueError("global_search 需要指定课题：它检索的是课题内的想法/实验/稿件")
    async with get_sessionmaker()() as session:
        hits = await search_service.global_search(session, project_id=ctx.project_id, q=q)
    return {"hits": [h.model_dump(mode="json") for h in hits]}
