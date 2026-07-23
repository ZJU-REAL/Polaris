"""论文图筛选注释（docs/api-lit.md §6.5）：stage=librarian 多模态挑重要图 + 中文图注。

确定性部分（读文件、合并、降级）为普通代码；只有「哪几张图重要 + 图注」交给 LLM。
LLM 解析失败重试 1 次，仍失败降级：按面积取前 4 张 important=true、caption=null。
"""

import asyncio
import io
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

DEGRADE_TOP_N = 4  # LLM 失败降级：按面积取前 N 张标记 important

# 视觉大模型（经 LiteLLM 代理的 Anthropic）对单图的硬限制：
# 任一边 ≤8000px、base64 数据 ≤5MB。base64 相对原始字节膨胀约 4/3，
# 故原始字节需 ≤ ~3.75MB，这里留余量取 3.5MB / 单边 7600px。
_MAX_IMAGE_DIM = 7600
_MAX_SENDABLE_BYTES = 3_500_000


def prepare_image_for_llm(data: bytes) -> bytes | None:
    """把一张图压到视觉大模型能接收的范围内（单边 ≤7600px、base64 ≤5MB）。

    - 已在限制内：原样返回（沿用旧行为，jpeg/png 皆可）。
    - 超尺寸或超体积：等比缩放并重编码为 PNG，必要时继续缩到体积达标。
    - 无法解码或压不下去：返回 None（调用方跳过该图，不阻断任务）。
    """
    from PIL import Image  # 延迟导入：仅在真正处理图片时需要

    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
    except Exception:  # noqa: BLE001 — 无法探测尺寸的图，仅在体积安全时才敢原样送
        return data if len(data) <= _MAX_SENDABLE_BYTES else None

    if max(width, height) <= _MAX_IMAGE_DIM and len(data) <= _MAX_SENDABLE_BYTES:
        return data

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:  # noqa: BLE001 — 解码失败无法安全缩放，丢弃
        return None

    if max(img.width, img.height) > _MAX_IMAGE_DIM:
        scale = _MAX_IMAGE_DIM / max(img.width, img.height)
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    for _ in range(6):  # 反复缩到 PNG 体积达标，最多 6 轮
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        out = buf.getvalue()
        if len(out) <= _MAX_SENDABLE_BYTES:
            return out
        new_size = (round(img.width * 0.8), round(img.height * 0.8))
        if min(new_size) < 8:
            break
        img = img.resize(new_size, Image.LANCZOS)
    return None


# 图片类型：动机 / 方法 / 架构 / 实验 / 其他（编译时决定插到哪个小节）
FIGURE_KINDS = ("motivation", "method", "architecture", "experiment", "other")
FIGURE_KIND_ZH = {
    "motivation": "动机图",
    "method": "方法图",
    "architecture": "架构图",
    "experiment": "实验图",
    "other": "其他",
}

FIGURE_ANNOTATE_SYSTEM_PROMPT = """\
你是文献图片评审，从一篇论文提取出的候选图里挑出对理解论文最关键的图，
给每张重要图标注类型并配中文说明。优先覆盖这四类（论文里有就选）：
- motivation：动机/问题示意图（说明为什么要做这件事）
- method：方法/流程图（核心思路怎么运转）
- architecture：模型/系统架构图
- experiment：核心实验结果或分析图
重要图通常 2-6 张；纯装饰图、logo、不影响理解的小图不要选。
只输出一个 JSON 数组，不要输出任何其他文字或 Markdown 代码块，格式：
[{"index": 候选图编号, "important": true, \
"kind": "motivation|method|architecture|experiment|other", \
"caption": "1-2 句中文说明：图里画了什么、说明了什么"}]
index 必须取自下面给出的候选编号；不重要的图可以不列出。
"""


def normalize_figure_kind(raw: Any) -> str | None:
    value = str(raw or "").strip().lower()
    return value if value in FIGURE_KINDS else None


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
        important = bool(sel.get("important", True)) if sel else False
        kind = normalize_figure_kind(sel.get("kind")) if sel else None
        merged.append(
            {
                "index": int(cand["index"]),
                "page": int(cand["page"]),
                "width": int(cand["width"]),
                "height": int(cand["height"]),
                "caption": str(caption) if caption else None,
                # 重要图缺类型时归 other（前端标签/编译分节指令都依赖 kind）
                "kind": kind or ("other" if important else None),
                "important": important,
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
            "kind": None,
            "important": int(c["index"]) in top,
        }
        for c in candidates
    ]


def figures_annotated(figures: list[dict[str, Any]] | None) -> bool:
    """是否已过筛选注释：提取初值全为 important=False/caption=None，任一非空即视为已注释。"""
    return any(f.get("important") or f.get("caption") for f in figures or [])


def important_figures_with_bytes(
    paper: Paper, limit: int = 6
) -> list[tuple[dict[str, Any], bytes]]:
    """取重要图及其 PNG bytes（图文编译用）：文件缺失跳过、超限图降采样、最多 limit 张。"""
    out: list[tuple[dict[str, Any], bytes]] = []
    for fig in paper.figures or []:
        if not fig.get("important"):
            continue
        path = figure_path(str(paper.id), int(fig["index"]))
        if not path.exists():
            continue
        data = prepare_image_for_llm(path.read_bytes())
        if data is None:
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
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
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
        data = prepare_image_for_llm(path.read_bytes())
        if data is None:
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
                    project_id=project_id,
                    library_id=library_id,
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
    project_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """PyMuPDF 提取候选图 → LLM 筛选注释 → 写 Paper.figures 并落库。

    调用方需先保证 paper.pdf_path 存在（无 PDF 由路由映射 404）。
    """
    candidates = await extract_figures(str(paper.id), Path(paper.pdf_path))
    figures = await annotate_figures(paper, candidates, user_id=user_id, project_id=project_id)
    await session.commit()
    return figures
