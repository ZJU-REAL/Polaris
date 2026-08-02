"""MCP 工具自检：用课题里的真实数据自动配样例参数，把每个工具真跑一遍。

为什么需要：工具 handler 依赖 ``services/*``，底层重构（签名变了、字段没了、
表结构改了）不会让 import 失败，只会在**调用时**炸——静态检查看不出来，
只有真跑一遍才知道哪个工具已经失效。

自检走的正是外部 MCP 客户端的同一条路径（``dispatch.call_tool``，含成员校验 +
project_id 注入），因此这里的结果就是 Claude Desktop / Cursor 会拿到的结果。

样例参数策略（``sample_arguments``）：
- 只填「能从课题里取到真实样本」的参数（论文/概念/想法/实验/稿件 id、检索词、图编号）；
- 过滤类可选参数（kind/category/status/format/k/page…）一律不填，避免把结果筛空；
- ``mode`` 固定取 keyword：确定性、且不触发 embedding（不烧钱、不受外部服务影响）；
- 样本缺失（比如库里没有论文）→ 该工具标 skipped 并说明原因，不算失败。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import false, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.dispatch import call_tool
from app.models.experiment import Experiment
from app.models.idea import Idea
from app.models.library_direction import LibraryPaper
from app.models.manuscript import Manuscript
from app.models.paper import Concept, Paper
from app.models.voyage import VoyageRun
from app.services.concepts import library_concept_ids
from app.services.libraries import get_source_library_ids
from app.tools import ToolSpec, list_tools

# 单个工具的超时：卡死算失败，不拖垮整轮自检（联网工具也共用这个上限）
TOOL_TIMEOUT = 60.0
_PREVIEW_CHARS = 400
_SAMPLE_PAPERS = 40  # 取多少篇论文来找样本（找带图的、带 DOI 的）
_PROBE_MAX_DIM = 512  # 自检取图时缩到小图：只验证能不能取出来，不需要原尺寸

# 样本缺失时给用户看的原因（大白话）
_MISSING_REASON = {
    "paper_id": "课题语料里还没有论文",
    "index": "库内没有已抽取图片的论文",
    "name": "库内还没有概念",
    "idea_id": "课题里还没有想法",
    "experiment_id": "课题里还没有实验",
    "manuscript_id": "课题里还没有稿件",
    "doi": "库内论文都没有 DOI",
    "library_id": "课题还没有关联任何文献库",
    "task_id": "课题里还没有跑过 AI 任务",
    "path": "课题里还没有稿件（拿不到文件路径）",
}

_FALLBACK_QUERY = "learning"


@dataclass(slots=True, frozen=True)
class ProjectSamples:
    """从课题里捞出来的样例数据，用于给工具配参数。"""

    query: str = _FALLBACK_QUERY
    paper_id: str | None = None
    figure_paper_id: str | None = None
    figure_index: int | None = None
    doi: str | None = None
    concept_name: str | None = None
    idea_id: str | None = None
    experiment_id: str | None = None
    manuscript_id: str | None = None
    library_id: str | None = None
    task_id: str | None = None
    manuscript_path: str | None = None


def _query_from_title(title: str | None) -> str:
    """从论文标题里取一个检索词（最长的字母词，退化到前 12 个字符）。"""
    if not title:
        return _FALLBACK_QUERY
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", title)]
    if words:
        return max(words, key=len)
    stripped = title.strip()
    return stripped[:12] or _FALLBACK_QUERY


def _pick_figure(paper: Paper) -> int | None:
    """论文里挑一张图的 index：优先「重要图」。"""
    figs = [f for f in (paper.figures or []) if isinstance(f, dict) and f.get("index") is not None]
    if not figs:
        return None
    chosen = next((f for f in figs if f.get("important")), figs[0])
    try:
        return int(chosen["index"])
    except (TypeError, ValueError):
        return None


async def collect_samples(session: AsyncSession, *, project_id: uuid.UUID) -> ProjectSamples:
    """扫一遍课题数据，凑出各类工具需要的样例入参。"""
    library_ids = await get_source_library_ids(session, project_id)

    papers: list[Paper] = []
    if library_ids:
        stmt = (
            select(Paper)
            .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
            .where(LibraryPaper.library_id.in_(library_ids))
            .order_by(LibraryPaper.created_at.desc())
            .limit(_SAMPLE_PAPERS)
        )
        papers = list((await session.execute(stmt)).scalars().unique())

    figure_paper_id: str | None = None
    figure_index: int | None = None
    for paper in papers:
        index = _pick_figure(paper)
        if index is not None:
            figure_paper_id, figure_index = str(paper.id), index
            break

    concept: Concept | None = None
    if library_ids:
        concept = (
            (
                await session.execute(
                    select(Concept)
                    .where(Concept.id.in_(library_concept_ids(library_ids)))
                    .order_by(Concept.name)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    async def _first_id(model: Any) -> str | None:
        row = (
            (
                await session.execute(
                    select(model.id)
                    .where(model.project_id == project_id, model.trashed_at.is_(None))
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        return str(row) if row is not None else None

    # 最近一次任务（含库任务：建库/增量抓取挂在库上，但课题语料正是它抓来的）
    task_stmt = (
        select(VoyageRun.id)
        .where(
            (VoyageRun.project_id == project_id)
            | (VoyageRun.library_id.in_(library_ids) if library_ids else false())
        )
        .order_by(VoyageRun.created_at.desc())
        .limit(1)
    )
    task_id = (await session.execute(task_stmt)).scalars().first()

    # 稿件文件路径：读文件类工具没有 path 就没法测，拿主文件当样本
    manuscript_id = await _first_id(Manuscript)
    manuscript_path: str | None = None
    if manuscript_id is not None:
        manuscript = await session.get(Manuscript, uuid.UUID(manuscript_id))
        manuscript_path = manuscript.main_tex if manuscript is not None else None

    return ProjectSamples(
        query=_query_from_title(papers[0].title if papers else None),
        paper_id=str(papers[0].id) if papers else None,
        figure_paper_id=figure_paper_id,
        figure_index=figure_index,
        doi=next((p.doi for p in papers if p.doi), None),
        concept_name=concept.name if concept is not None else None,
        idea_id=await _first_id(Idea),
        experiment_id=await _first_id(Experiment),
        manuscript_id=manuscript_id,
        library_id=str(library_ids[0]) if library_ids else None,
        task_id=str(task_id) if task_id is not None else None,
        manuscript_path=manuscript_path,
    )


def _sample_value(
    name: str, schema: dict[str, Any], samples: ProjectSamples, *, prefer_figure_paper: bool
) -> Any:
    """按参数名给一个样例值；给不出（没有样本 / 属于过滤类参数）返回 None。"""
    if name == "paper_id":
        if prefer_figure_paper:
            return samples.figure_paper_id or samples.paper_id
        return samples.paper_id
    if name in ("query", "q"):
        return samples.query
    if name == "name":
        return samples.concept_name
    if name == "index":
        return samples.figure_index
    if name == "doi":
        return samples.doi
    if name in ("idea_id", "experiment_id", "manuscript_id", "library_id", "task_id"):
        return getattr(samples, name)
    if name == "path":
        return samples.manuscript_path
    if name == "mode" and "keyword" in (schema.get("enum") or []):
        return "keyword"  # 确定性、不触发 embedding
    if name == "max_dim":
        return _PROBE_MAX_DIM
    return None  # 过滤类可选参数（kind/category/status/format/k/page…）不填


def sample_arguments(
    spec: ToolSpec, samples: ProjectSamples
) -> tuple[dict[str, Any] | None, str | None]:
    """给工具配一组样例参数；必填项没有样本时返回 ``(None, 跳过原因)``。"""
    props: dict[str, Any] = spec.input_schema.get("properties") or {}
    required = set(spec.input_schema.get("required") or [])
    # 图片类工具用「有图的论文」当样本，否则随便一篇都行
    prefer_figure_paper = "index" in props or "figure" in spec.name

    args: dict[str, Any] = {}
    for name, schema in props.items():
        value = _sample_value(name, schema or {}, samples, prefer_figure_paper=prefer_figure_paper)
        if value is None:
            if name in required:
                return None, _MISSING_REASON.get(name, f"没有可用的样例参数 {name}")
            continue
        args[name] = value

    # 引文类工具两个入参都可选，但至少得给一个，否则工具自己会报错
    if spec.name in ("get_references", "get_citations") and not args.get("paper_id"):
        return None, _MISSING_REASON["paper_id"]
    return args, None


def _summarize_content(result: dict[str, Any]) -> tuple[str, int]:
    """从 MCP 返回的 content blocks 里取「首个文本 + 图片张数」。"""
    text = ""
    images = 0
    for block in result.get("content") or []:
        if block.get("type") == "text" and not text:
            text = str(block.get("text") or "")
        elif block.get("type") == "image":
            images += 1
    return text, images


async def check_tool(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    spec: ToolSpec,
    samples: ProjectSamples,
) -> dict[str, Any]:
    """跑一个工具并给出 ok / error / skipped 结论。"""
    base = {"name": spec.name, "network": spec.network}
    args, skip_reason = sample_arguments(spec, samples)
    if args is None:
        return {**base, "status": "skipped", "arguments": None, "detail": skip_reason}

    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            call_tool(
                session,
                user_id=user_id,
                name=spec.name,
                arguments={**args, "project_id": str(project_id)},
            ),
            TOOL_TIMEOUT,
        )
    except TimeoutError:
        return {
            **base,
            "status": "error",
            "arguments": args,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "detail": f"超时（>{TOOL_TIMEOUT:.0f}s）",
        }
    except Exception as e:  # noqa: BLE001 — 未捕获异常正是「工具已失效」的信号
        return {
            **base,
            "status": "error",
            "arguments": args,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "detail": f"{type(e).__name__}: {e}",
        }

    duration_ms = round((time.perf_counter() - started) * 1000)
    text, images = _summarize_content(result)
    if result.get("isError"):
        return {
            **base,
            "status": "error",
            "arguments": args,
            "duration_ms": duration_ms,
            "detail": text,
        }
    return {
        **base,
        "status": "ok",
        "arguments": args,
        "duration_ms": duration_ms,
        "preview": text[:_PREVIEW_CHARS],
        "images": images,
    }


async def run_selfcheck(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    include_network: bool = False,
    names: list[str] | None = None,
) -> dict[str, Any]:
    """把工具目录整体跑一遍。串行执行：一个 AsyncSession 不能并发使用。

    联网工具默认只标 skipped（真跑会打外部接口、受限速影响），
    ``include_network=True`` 时才实际调用。
    """
    samples = await collect_samples(session, project_id=project_id)
    results: list[dict[str, Any]] = []
    for spec in list_tools(names):
        if spec.network and not include_network:
            results.append(
                {
                    "name": spec.name,
                    "network": True,
                    "status": "skipped",
                    "arguments": None,
                    "detail": "默认不测联网工具（会真的请求外部文献接口）",
                }
            )
            continue
        results.append(
            await check_tool(
                session, user_id=user_id, project_id=project_id, spec=spec, samples=samples
            )
        )

    counts = {"ok": 0, "error": 0, "skipped": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "project_id": str(project_id),
        "include_network": include_network,
        "samples": {
            "paper_id": samples.paper_id,
            "figure_paper_id": samples.figure_paper_id,
            "figure_index": samples.figure_index,
            "concept_name": samples.concept_name,
            "idea_id": samples.idea_id,
            "experiment_id": samples.experiment_id,
            "manuscript_id": samples.manuscript_id,
            "query": samples.query,
        },
        "summary": {"total": len(results), **counts},
        "results": results,
    }
