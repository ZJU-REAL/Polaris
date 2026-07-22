"""外部文献只读工具：库外检索、引文/参考文献、按 id 查元数据（走 arxiv/S2/OpenAlex）。

全部访问外部 HTTP API（``network=True``），带 Redis 缓存 + 限速；失败抛 ``ValueError``
（内部循环转成回给 LLM 的错误消息，外部 MCP 转成 tool error）。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.db import get_sessionmaker
from app.models.paper import Paper
from app.services.libraries import membership_for_project
from app.services.literature import get_openalex_client, get_s2_client
from app.tools.context import ToolContext
from app.tools.registry import tool

_MAX_K = 10


def _s2_brief(row: dict[str, Any]) -> dict[str, Any]:
    authors = [a.get("name") for a in (row.get("authors") or []) if isinstance(a, dict)]
    return {
        "title": row.get("title"),
        "year": row.get("year"),
        "authors": [a for a in authors if a][:12],
        "abstract": (row.get("abstract") or "")[:600] or None,
        "url": row.get("url"),
        "external_ids": row.get("externalIds"),
        "citation_count": row.get("citationCount"),
    }


@tool(
    "external_search",
    description="库外文献检索（Semantic Scholar，失败降级 OpenAlex）——找库里还没有的论文",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "题目/关键词"},
            "k": {"type": "integer", "minimum": 1, "maximum": _MAX_K, "default": 5},
        },
        "required": ["query"],
    },
    network=True,
    summarize=lambda a, r: f"库外检索「{a.get('query', '')}」→ {len(r.get('results') or [])} 篇",
)
async def external_search(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("external_search 需要非空 query")
    k = min(_MAX_K, max(1, int(args.get("k") or 5)))
    try:
        rows = await get_s2_client().search_papers(query, limit=k)
        if rows:
            return {"source": "semantic_scholar", "results": [_s2_brief(r) for r in rows]}
    except Exception:  # noqa: BLE001 — S2 不可用则降级 OpenAlex
        rows = []
    works = await get_openalex_client().search_works(query, limit=k)
    return {
        "source": "openalex",
        "results": [
            {
                "title": w.get("title"),
                "year": w.get("publication_year"),
                "url": (w.get("primary_location") or {}).get("landing_page_url"),
                "doi": w.get("doi"),
                "citation_count": w.get("cited_by_count"),
            }
            for w in works
        ],
    }


async def _resolve_ref(ctx: ToolContext, args: dict[str, Any]) -> str:
    """把入参解析成 S2 可识别的 id：优先显式 paper_ref，其次库内 paper_id → arXiv/DOI。"""
    ref = str(args.get("paper_ref") or "").strip()
    if ref:
        return ref
    raw = str(args.get("paper_id") or "").strip()
    if not raw:
        raise ValueError("需要 paper_ref（arXiv:xxx / DOI:xxx / S2 id）或库内 paper_id")
    try:
        pid = uuid.UUID(raw)
    except ValueError as e:
        raise ValueError(f"paper_id 不是合法 uuid：{raw}") from e
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, pid)
        if paper is None or (
            await membership_for_project(session, project_id=ctx.project_id, paper_id=pid)
        ) is None:
            raise ValueError(f"库内不存在该论文：{raw}")
        if paper.arxiv_id:
            return f"arXiv:{paper.arxiv_id}"
        if paper.doi:
            return f"DOI:{paper.doi}"
    raise ValueError("该论文没有 arXiv/DOI 外部标识，无法查引文")


_REF_SCHEMA = {
    "type": "object",
    "properties": {
        "paper_ref": {"type": "string", "description": "外部 id：arXiv:xxx / DOI:xxx / S2 id"},
        "paper_id": {"type": "string", "description": "或库内论文 uuid（自动解析为外部 id）"},
    },
}


@tool(
    "get_references",
    description="某论文引用的参考文献（它站在谁的肩膀上）",
    input_schema=_REF_SCHEMA,
    network=True,
    summarize=lambda a, r: f"参考文献 → {len(r.get('references') or [])} 篇",
)
async def get_references(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ref = await _resolve_ref(ctx, args)
    rows = await get_s2_client().get_references(ref, limit=50)
    return {"paper_ref": ref, "references": [_s2_brief(r) for r in rows]}


@tool(
    "get_citations",
    description="引用某论文的后续工作（谁在它基础上继续做）",
    input_schema=_REF_SCHEMA,
    network=True,
    summarize=lambda a, r: f"施引文献 → {len(r.get('citations') or [])} 篇",
)
async def get_citations(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ref = await _resolve_ref(ctx, args)
    rows = await get_s2_client().get_citations(ref, limit=50)
    return {"paper_ref": ref, "citations": [_s2_brief(r) for r in rows]}


@tool(
    "lookup_paper",
    description="按 DOI 查库外论文元数据（含被引数）",
    input_schema={
        "type": "object",
        "properties": {"doi": {"type": "string", "description": "DOI"}},
        "required": ["doi"],
    },
    network=True,
    summarize=lambda a, r: f"查元数据：{r.get('title') or a.get('doi', '')}",
)
async def lookup_paper(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    doi = str(args.get("doi") or "").strip()
    if not doi:
        raise ValueError("lookup_paper 需要 doi")
    work = await get_openalex_client().get_by_doi(doi)
    if not work:
        return {"doi": doi, "found": False}
    return {
        "doi": doi,
        "found": True,
        "title": work.get("title"),
        "year": work.get("publication_year"),
        "citation_count": work.get("cited_by_count"),
        "url": (work.get("primary_location") or {}).get("landing_page_url"),
    }
