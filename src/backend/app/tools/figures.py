"""围绕 Paper 的图片与支撑只读工具（docs/api-mcp.md）。

核心：把论文里已抽取、已分类（motivation/method/architecture/experiment/other）、
已配中文图注的图片，作为 MCP image content 暴露出去——外部客户端（Claude Code /
Cursor）可直接取「方法图 / 实验图」做 PPT。图片工具返回 ``ToolResult``（文本 + 图片）。
支撑工具（引用/笔记/划线/相关论文）为纯文本 dict。
"""

from __future__ import annotations

import io
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy.orm import selectinload

from app.core.db import get_sessionmaker
from app.models.paper import Paper
from app.services import citations as citations_service
from app.services import concepts as concepts_service
from app.services import highlights as highlights_service
from app.services import notes as notes_service
from app.services.libraries import membership_for_project
from app.services.literature.pdf_extract import figure_path
from app.tools.context import ToolContext
from app.tools.literature import search_papers as _search_papers
from app.tools.registry import ToolImage, ToolResult, tool

FIGURE_KINDS = ["motivation", "method", "architecture", "experiment", "other"]
_DEFAULT_MAX_DIM = 1600
_MAX_BATCH = 8


async def _project_paper(
    session: Any, ctx: ToolContext, raw_id: Any, *, with_concepts: bool = False
) -> Paper:
    try:
        pid = uuid.UUID(str(raw_id))
    except ValueError as e:
        raise ValueError(f"paper_id 不是合法 uuid：{raw_id}") from e
    opts = [selectinload(Paper.concepts)] if with_concepts else None
    paper = await session.get(Paper, pid, options=opts)
    membership = (
        await membership_for_project(session, project_id=ctx.project_id, paper_id=pid)
        if paper is not None
        else None
    )
    if paper is None or membership is None:
        raise ValueError(f"库内不存在该论文：{raw_id}")
    return paper


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


def _png_bytes(path: Path, max_dim: int) -> bytes:
    """读取图片并等比缩到单边 ≤ max_dim，重编码 PNG（控制 MCP payload 体积）。"""
    from PIL import Image

    with Image.open(path) as im:
        im.load()
        if max(im.width, im.height) > max_dim:
            scale = max_dim / max(im.width, im.height)
            im = im.resize(
                (max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                Image.LANCZOS,
            )
        if im.mode not in ("RGB", "L", "RGBA"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def _clamp_max_dim(raw: Any) -> int:
    try:
        return max(256, min(4096, int(raw)))
    except (ValueError, TypeError):
        return _DEFAULT_MAX_DIM


@tool(
    "list_paper_figures",
    description="列出某论文所有图的元数据（index/页码/类型/图注），不含图片本体",
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
            "figures": [_fig_meta(f) for f in figs],
        }


@tool(
    "get_paper_figure",
    description="取某论文某张图的图片（PNG）+ 图注，用于 PPT / 展示 / 视觉说明",
    input_schema={
        "type": "object",
        "properties": {
            "paper_id": {"type": "string", "description": "论文 uuid"},
            "index": {"type": "integer", "description": "图编号（见 list_paper_figures）"},
            "max_dim": {"type": "integer", "description": "单边最大像素，默认 1600"},
        },
        "required": ["paper_id", "index"],
    },
    summarize=lambda a, r: f"取图 #{a.get('index')}：{r.get('caption') or r.get('title', '')}",
)
async def get_paper_figure(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    index = int(args.get("index")) if str(args.get("index", "")).lstrip("-").isdigit() else None
    if index is None:
        raise ValueError("get_paper_figure 需要整数 index")
    max_dim = _clamp_max_dim(args.get("max_dim"))
    async with get_sessionmaker()() as session:
        paper = await _project_paper(session, ctx, args.get("paper_id"))
        fig = next((f for f in (paper.figures or []) if int(f["index"]) == index), None)
        if fig is None:
            raise ValueError(f"论文无此图：index={index}")
        path = figure_path(str(paper.id), index)
        if not path.exists():
            raise ValueError(f"图片文件缺失：index={index}")
        data = _png_bytes(path, max_dim)
        payload = {
            "paper_id": str(paper.id),
            "title": paper.title,
            **_fig_meta(fig),
        }
    return ToolResult(payload=payload, images=(ToolImage(data=data, label=fig.get("caption")),))


@tool(
    "get_paper_figures",
    description="批量取某论文的图片（默认只取重要图，可按 kind 过滤）——做 PPT 一次拿全套",
    input_schema={
        "type": "object",
        "properties": {
            "paper_id": {"type": "string", "description": "论文 uuid"},
            "kind": {"type": "string", "enum": FIGURE_KINDS, "description": "只取某类型（可选）"},
            "only_important": {"type": "boolean", "description": "只取重要图，默认 true"},
            "max_dim": {"type": "integer", "description": "单边最大像素，默认 1600"},
        },
        "required": ["paper_id"],
    },
    summarize=lambda a, r: f"批量取图：{len(r.get('figures') or [])} 张",
)
async def get_paper_figures(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    kind = str(args.get("kind") or "").strip() or None
    only_important = args.get("only_important", True) is not False
    max_dim = _clamp_max_dim(args.get("max_dim"))
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
        try:
            images.append(ToolImage(data=_png_bytes(path, max_dim), label=fig.get("caption")))
            metas.append(_fig_meta(fig))
        except Exception:  # noqa: BLE001 — 单图解码失败跳过，不阻断其余
            continue
    return ToolResult(
        payload={"paper_id": paper_id, "title": title, "figures": metas}, images=tuple(images)
    )


@tool(
    "find_figures",
    description="跨库按主题/类型找图（返回图元数据，再用 get_paper_figure 取图本体）",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "主题关键词，如 检索增强 方法图"},
            "kind": {"type": "string", "enum": FIGURE_KINDS, "description": "只找某类型（可选）"},
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

    # 复用库内论文检索，再从命中论文里收重要图
    search = await _search_papers(ctx, {"query": query, "k": 8})
    paper_ids = [uuid.UUID(p["paper_id"]) for p in search.get("results", [])]
    out: list[dict[str, Any]] = []
    async with get_sessionmaker()() as session:
        for pid in paper_ids:
            paper = await session.get(Paper, pid)
            if paper is None or (
                await membership_for_project(session, project_id=ctx.project_id, paper_id=pid)
            ) is None:
                continue
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
    description="取某论文的引用条目（BibTeX 或 CSL-JSON），用于 PPT / 论文署名",
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
    description="取某论文下当前用户的笔记（P5b 起笔记仅作者本人可见）",
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
            await notes_service.list_paper_notes(
                session, paper_id=paper.id, author_id=ctx.user_id
            )
            if ctx.user_id is not None
            else []
        )
        return {
            "paper_id": str(paper.id),
            "notes": [{"author": author, "content": note.content} for note, author in rows],
        }


@tool(
    "get_paper_highlights",
    description="取某论文下当前用户的划线/高亮（含所在页与选中文本；仅作者本人可见）",
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
    description="与某论文共享概念最多的近邻论文（扩展调研）",
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
