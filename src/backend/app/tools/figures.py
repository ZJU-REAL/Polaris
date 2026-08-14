"""围绕 Paper 的图片与支撑只读工具（docs/mcp.md）。

论文图片对 MCP 返回短期签名下载链接，不内联 base64；内部 Buddy 仍通过 ``ToolImage``
引用把图片交给前端显示。支撑工具（引用/笔记/划线/相关论文）为纯文本 dict。
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from app.core.db import get_sessionmaker
from app.models.paper import Paper
from app.services import citations as citations_service
from app.services import concepts as concepts_service
from app.services import highlights as highlights_service
from app.services import notes as notes_service
from app.services.literature.pdf_extract import figure_path
from app.services.paper_figure_downloads import create_download_link
from app.tools.context import ToolContext
from app.tools.literature import search_papers as _search_papers
from app.tools.registry import ToolImage, ToolResult, tool
from app.tools.scope import paper_access, readable_paper

FIGURE_KINDS = ["motivation", "method", "architecture", "experiment", "other"]
_MAX_BATCH = 8


async def _project_paper(
    session: Any, ctx: ToolContext, raw_id: Any, *, with_concepts: bool = False
) -> Paper:
    access = await readable_paper(session, ctx, raw_id, with_concepts=with_concepts)
    return access.view.paper


def _figure_ref(paper_id: uuid.UUID, index: int) -> dict[str, Any]:
    """图片在平台里的出处。前端拿它去 /papers/{id}/figures/{index}/image 取图——
    那个端点本来就有、且带鉴权，对话流里没必要再造一条图片通道。"""
    return {"kind": "paper_figure", "paper_id": str(paper_id), "index": index}


def _fig_meta(fig: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": fig.get("index"),
        "page": fig.get("page"),
        "kind": fig.get("kind"),
        "caption": fig.get("caption"),
        "important": bool(fig.get("important")),
        "width": fig.get("width"),
        "height": fig.get("height"),
    }


def _download_fields(ctx: ToolContext, paper_id: uuid.UUID, index: int) -> dict[str, str | None]:
    """返回下载地址；无用户身份的内部任务退回需要 JWT 的相对 API 地址。"""
    if ctx.user_id is None:
        return {
            "download_url": f"/api/papers/{paper_id}/figures/{index}/image",
            "download_url_expires_at": None,
        }
    link = create_download_link(
        user_id=ctx.user_id,
        paper_id=paper_id,
        index=index,
        base_url=ctx.base_url,
    )
    return {
        "download_url": link.url,
        "download_url_expires_at": link.expires_at,
    }


def _download_fields_if_available(
    ctx: ToolContext,
    paper_id: uuid.UUID,
    index: int,
) -> dict[str, str | None]:
    """只为实际存在的图片签发链接，避免清单暴露必然返回 404 的地址。"""
    if not figure_path(str(paper_id), index).exists():
        return {"download_url": None, "download_url_expires_at": None}
    return _download_fields(ctx, paper_id, index)


@tool(
    "list_paper_figures",
    description="列出论文全部插图的元数据和下载链接；链接短期有效，不内联图片本体",
    input_schema={
        "type": "object",
        "properties": {"paper_id": {"type": "string", "description": "论文 uuid"}},
        "required": ["paper_id"],
    },
    summarize=lambda a, r: f"图清单：{len(r.get('figures') or [])} 张",
)
async def list_paper_figures(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        paper = await _project_paper(session, ctx, args.get("paper_id"))
        figs = paper.figures or []
        return {
            "paper_id": str(paper.id),
            "title": paper.title,
            "figures": [
                {
                    **_fig_meta(fig),
                    **_download_fields_if_available(
                        ctx,
                        paper.id,
                        int(fig["index"]),
                    ),
                }
                for fig in figs
            ],
        }


@tool(
    "get_paper_figure",
    description="取某篇论文指定编号的插图，返回短期有效的 PNG 下载链接与图注，不内联图片",
    input_schema={
        "type": "object",
        "properties": {
            "paper_id": {"type": "string", "description": "论文 uuid"},
            "index": {"type": "integer", "description": "图编号（见 list_paper_figures）"},
        },
        "required": ["paper_id", "index"],
    },
    summarize=lambda a, r: f"取图 #{a.get('index')}：{r.get('caption') or r.get('title', '')}",
)
async def get_paper_figure(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    index = int(args.get("index")) if str(args.get("index", "")).lstrip("-").isdigit() else None
    if index is None:
        raise ValueError("get_paper_figure 需要整数 index")
    async with get_sessionmaker()() as session:
        paper = await _project_paper(session, ctx, args.get("paper_id"))
        fig = next((f for f in (paper.figures or []) if int(f["index"]) == index), None)
        if fig is None:
            raise ValueError(f"论文无此图：index={index}")
        path = figure_path(str(paper.id), index)
        if not path.exists():
            raise ValueError(f"图片文件缺失：index={index}")
        payload = {
            "paper_id": str(paper.id),
            "title": paper.title,
            **_fig_meta(fig),
            **_download_fields(ctx, paper.id, index),
        }
    return ToolResult(
        payload=payload,
        images=(
            ToolImage(
                label=fig.get("caption"),
                ref=_figure_ref(paper.id, int(fig["index"])),
            ),
        ),
    )


@tool(
    "get_paper_figures",
    description="批量取论文插图的短期下载链接，默认只取重要图，可按类型筛选",
    input_schema={
        "type": "object",
        "properties": {
            "paper_id": {"type": "string", "description": "论文 uuid"},
            "kind": {"type": "string", "enum": FIGURE_KINDS, "description": "只取某类型（可选）"},
            "only_important": {"type": "boolean", "description": "只取重要图，默认 true"},
        },
        "required": ["paper_id"],
    },
    summarize=lambda a, r: f"批量取图：{len(r.get('figures') or [])} 张",
)
async def get_paper_figures(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    kind = str(args.get("kind") or "").strip() or None
    only_important = args.get("only_important", True) is not False
    async with get_sessionmaker()() as session:
        paper = await _project_paper(session, ctx, args.get("paper_id"))
        selected: list[dict[str, Any]] = []
        for fig in paper.figures or []:
            if only_important and not fig.get("important"):
                continue
            if kind and fig.get("kind") != kind:
                continue
            selected.append(fig)
            if len(selected) >= _MAX_BATCH:
                break
        paper_id = str(paper.id)
        title = paper.title

    metas: list[dict[str, Any]] = []
    images: list[ToolImage] = []
    for fig in selected:
        path = figure_path(paper_id, int(fig["index"]))
        if not path.exists():
            continue
        index = int(fig["index"])
        paper_uuid = uuid.UUID(paper_id)
        images.append(
            ToolImage(
                label=fig.get("caption"),
                ref=_figure_ref(paper_uuid, index),
            )
        )
        metas.append(
            {
                **_fig_meta(fig),
                **_download_fields(ctx, paper_uuid, index),
            }
        )
    return ToolResult(
        payload={"paper_id": paper_id, "title": title, "figures": metas}, images=tuple(images)
    )


@tool(
    "find_figures",
    description=(
        "在本课题语料内按主题或类型检索插图，返回图片元数据；取图片本体请用 get_paper_figure"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "主题关键词，如 检索增强 方法图"},
            "kind": {"type": "string", "enum": FIGURE_KINDS, "description": "只找某类型（可选）"},
            "mode": {"type": "string", "enum": ["keyword", "semantic"], "default": "semantic"},
            "k": {"type": "integer", "minimum": 1, "maximum": 20, "description": "最多图数,默认8"},
        },
        "required": ["query"],
    },
    summarize=lambda a, r: f"找图「{a.get('query', '')}」→ {len(r.get('figures') or [])} 张",
)
async def find_figures(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("find_figures 需要非空 query")
    kind = str(args.get("kind") or "").strip() or None
    k = max(1, min(20, int(args.get("k") or 8)))
    mode = str(args.get("mode") or "semantic")

    # 复用库内论文检索，再从命中论文里收重要图（mode 透传：keyword 不碰 embedding）
    search = await _search_papers(ctx, {"query": query, "k": 8, "mode": mode})
    paper_ids = [uuid.UUID(p["paper_id"]) for p in search.get("results", [])]
    out: list[dict[str, Any]] = []
    async with get_sessionmaker()() as session:
        for pid in paper_ids:
            access = await paper_access(session, ctx, pid)
            if access is None:
                continue
            paper = access.view
            for fig in paper.figures or []:
                if not fig.get("important"):
                    continue
                if kind and fig.get("kind") != kind:
                    continue
                out.append(
                    {
                        "paper_id": str(paper.id),
                        "title": paper.title,
                        **_fig_meta(fig),
                    }
                )
                if len(out) >= k:
                    break
            if len(out) >= k:
                break
    return {"query": query, "figures": out}


@tool(
    "get_paper_citation",
    description="取某篇论文的引用条目，格式为 BibTeX 或 CSL-JSON",
    input_schema={
        "type": "object",
        "properties": {
            "paper_id": {"type": "string", "description": "论文 uuid"},
            "format": {"type": "string", "enum": ["bibtex", "csl"], "description": "默认 bibtex"},
        },
        "required": ["paper_id"],
    },
    summarize=lambda a, r: f"引用条目（{r.get('format', 'bibtex')}）",
)
async def get_paper_citation(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    fmt = str(args.get("format") or "bibtex").strip()
    async with get_sessionmaker()() as session:
        paper = await _project_paper(session, ctx, args.get("paper_id"))
        pid = str(paper.id)
        if fmt == "csl":
            csl = citations_service.build_csl_json([paper])
            return {"paper_id": pid, "format": "csl", "csl": csl}
        bibtex = citations_service.build_bibtex([paper])
        return {"paper_id": pid, "format": "bibtex", "bibtex": bibtex}


@tool(
    "get_paper_notes",
    description="取当前用户在某篇论文下的笔记；笔记仅作者本人可见",
    input_schema={
        "type": "object",
        "properties": {"paper_id": {"type": "string", "description": "论文 uuid"}},
        "required": ["paper_id"],
    },
    summarize=lambda a, r: f"笔记：{len(r.get('notes') or [])} 条",
)
async def get_paper_notes(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        paper = await _project_paper(session, ctx, args.get("paper_id"))
        # 笔记仅作者本人可见：无用户语境（系统内部调用）时返回空
        rows = (
            await notes_service.list_paper_notes(session, paper_id=paper.id, author_id=ctx.user_id)
            if ctx.user_id is not None
            else []
        )
        return {
            "paper_id": str(paper.id),
            "notes": [{"author": author, "content": note.content} for note, author in rows],
        }


@tool(
    "get_paper_highlights",
    description="取当前用户在某篇论文下的划线，含所在页码与选中文本；划线仅作者本人可见",
    input_schema={
        "type": "object",
        "properties": {"paper_id": {"type": "string", "description": "论文 uuid"}},
        "required": ["paper_id"],
    },
    summarize=lambda a, r: f"划线：{len(r.get('highlights') or [])} 处",
)
async def get_paper_highlights(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        paper = await _project_paper(session, ctx, args.get("paper_id"))
        rows = (
            await highlights_service.list_paper_highlights(
                session, paper_id=paper.id, author_id=ctx.user_id
            )
            if ctx.user_id is not None
            else []
        )
        return {
            "paper_id": str(paper.id),
            "highlights": [
                {
                    "page": hl.page,
                    "text": hl.selected_text,
                    "note": hl.note,
                    "author": author,
                }
                for hl, author in rows
            ],
        }


@tool(
    "related_papers",
    description="列出与某篇论文共享概念最多的近邻论文",
    input_schema={
        "type": "object",
        "properties": {
            "paper_id": {"type": "string", "description": "论文 uuid"},
            "k": {"type": "integer", "minimum": 1, "maximum": 20, "description": "默认 8"},
        },
        "required": ["paper_id"],
    },
    summarize=lambda a, r: f"相关论文：{len(r.get('related') or [])} 篇",
)
async def related_papers(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    k = max(1, min(20, int(args.get("k") or 8)))
    async with get_sessionmaker()() as session:
        paper = await _project_paper(session, ctx, args.get("paper_id"), with_concepts=True)
        counter: Counter[uuid.UUID] = Counter()
        titles: dict[uuid.UUID, Paper] = {}
        for concept in paper.concepts:
            for p in await concepts_service.papers_of_concept(session, concept.id):
                if p.id == paper.id:
                    continue
                counter[p.id] += 1
                titles[p.id] = p
        ranked = counter.most_common(k)
        return {
            "paper_id": str(paper.id),
            "related": [
                {
                    "paper_id": str(pid),
                    "title": titles[pid].title,
                    "year": titles[pid].year,
                    "shared_concepts": n,
                }
                for pid, n in ranked
            ],
        }
