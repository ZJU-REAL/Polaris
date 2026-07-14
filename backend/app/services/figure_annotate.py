"""论文图筛选注释（docs/api-lit.md §6.5）：stage=librarian 多模态挑重要图 + 中文图注。

确定性部分（读文件、合并、降级）为普通代码；只有「哪几张图重要 + 图注」交给 LLM。
LLM 解析失败重试 1 次，仍失败降级：按面积取前 4 张 important=true、caption=null。
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter, get_llm_router
from app.models.paper import Paper
from app.services.literature.pdf_extract import extract_figures, figure_path

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 单图超过 4MB 不送 LLM（仍保留在 figures 里）
DEGRADE_TOP_N = 4  # LLM 失败降级：按面积取前 N 张标记 important

FIGURE_ANNOTATE_SYSTEM_PROMPT = """\
你是文献图片评审，从一篇论文提取出的候选图里挑出对理解论文最关键的图
（方法/架构图、核心结果图等，重要的通常 2-4 张），并为每张重要图配一句中文说明。
只输出一个 JSON 数组，不要输出任何其他文字或 Markdown 代码块，格式：
[{"index": 候选图编号, "important": true, "caption": "一句中文说明"}]
index 必须取自下面给出的候选编号；不重要的图可以不列出。
"""


def _extract_json_array(content: str) -> list[Any]:
    start = content.find("[")
    end = content.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("no JSON array found")
    data = json.loads(content[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("expected a JSON array")
    return data


def _area(candidate: dict[str, Any]) -> int:
    return int(candidate.get("width") or 0) * int(candidate.get("height") or 0)


def _merge(candidates: list[dict[str, Any]], selections: list[Any]) -> list[dict[str, Any]]:
    """LLM 筛选结果按 index 合并回候选列表；未提及的图 important=false。"""
    by_index: dict[int, dict[str, Any]] = {}
    for item in selections:
        if isinstance(item, dict) and isinstance(item.get("index"), int | float | str):
            try:
                by_index[int(item["index"])] = item
            except (TypeError, ValueError):
                continue
    merged: list[dict[str, Any]] = []
    for cand in candidates:
        sel = by_index.get(int(cand["index"]))
        caption = sel.get("caption") if sel else None
        merged.append(
            {
                "index": int(cand["index"]),
                "page": int(cand["page"]),
                "width": int(cand["width"]),
                "height": int(cand["height"]),
                "caption": str(caption) if caption else None,
                "important": bool(sel.get("important", True)) if sel else False,
            }
        )
    return merged


def _degrade(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """降级：面积前 N 张 important=true、caption=null，其余 important=false。"""
    top = {
        int(c["index"])
        for c in sorted(candidates, key=lambda c: (-_area(c), int(c["index"])))[:DEGRADE_TOP_N]
    }
    return [
        {
            "index": int(c["index"]),
            "page": int(c["page"]),
            "width": int(c["width"]),
            "height": int(c["height"]),
            "caption": None,
            "important": int(c["index"]) in top,
        }
        for c in candidates
    ]


def figures_annotated(figures: list[dict[str, Any]] | None) -> bool:
    """是否已过筛选注释：提取初值全为 important=False/caption=None，任一非空即视为已注释。"""
    return any(f.get("important") or f.get("caption") for f in figures or [])


def important_figures_with_bytes(
    paper: Paper, limit: int = 4
) -> list[tuple[dict[str, Any], bytes]]:
    """取重要图及其 PNG bytes（图文编译用）：文件缺失跳过、单张 >4MB 跳过、最多 limit 张。"""
    out: list[tuple[dict[str, Any], bytes]] = []
    for fig in paper.figures or []:
        if not fig.get("important"):
            continue
        path = figure_path(str(paper.id), int(fig["index"]))
        if not path.exists():
            continue
        data = path.read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            continue
        out.append((fig, data))
        if len(out) >= limit:
            break
    return out


def _build_user_prompt(paper: Paper, sendable: list[dict[str, Any]]) -> str:
    lines = [
        f"标题：{paper.title}",
        f"摘要：{paper.abstract or '（无摘要）'}",
        "候选图（与附带图片顺序一致）：",
    ]
    lines += [
        f"- index={c['index']}：第 {c['page']} 页，{c['width']}×{c['height']}" for c in sendable
    ]
    return "\n".join(lines)


async def annotate_figures(
    paper: Paper,
    candidates: list[dict[str, Any]],
    *,
    llm: LLMRouter | None = None,
    user_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """挑重要图并配中文图注，合并结果写 Paper.figures 并返回（调用方负责 commit）。"""
    llm = llm or get_llm_router()
    sendable: list[dict[str, Any]] = []
    images: list[bytes] = []
    for cand in candidates:
        path = figure_path(str(paper.id), int(cand["index"]))
        if not path.exists():
            continue
        data = path.read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            continue
        sendable.append(cand)
        images.append(data)

    merged: list[dict[str, Any]] | None = None
    if sendable:
        messages = [
            Message(role="system", content=FIGURE_ANNOTATE_SYSTEM_PROMPT),
            Message(role="user", content=_build_user_prompt(paper, sendable)),
        ]
        for attempt in range(2):  # 解析失败重试 1 次
            try:
                result = await llm.complete(
                    "librarian",
                    messages,
                    images=images,
                    user_id=user_id,
                    project_id=paper.project_id,
                    voyage_id=voyage_id,
                )
                merged = _merge(candidates, _extract_json_array(result.content))
                break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 调用/解析失败重试，仍失败走降级
                logger.warning(
                    "figure annotation attempt %d failed for paper %s",
                    attempt + 1,
                    paper.id,
                    exc_info=True,
                )
    if merged is None:
        merged = _degrade(candidates)
    paper.figures = merged
    return merged


async def extract_and_annotate(
    session: AsyncSession,
    paper: Paper,
    *,
    user_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """PyMuPDF 提取候选图 → LLM 筛选注释 → 写 Paper.figures 并落库。

    调用方需先保证 paper.pdf_path 存在（无 PDF 由路由映射 404）。
    """
    candidates = await extract_figures(str(paper.id), Path(paper.pdf_path))
    figures = await annotate_figures(paper, candidates, user_id=user_id)
    await session.commit()
    return figures
