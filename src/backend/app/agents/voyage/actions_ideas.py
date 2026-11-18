"""idea forge / review 动作（Voyage kinds ``idea_forge`` / ``idea_review`` 的固定计划执行体）。

forge 流水线（docs/task-system.md §7，Idea 2.0 信号升级见 docs/task-system.md §7）：
    forge.read_context → forge.collect_signals → forge.gap_analysis →
    forge.generate → forge.score → forge.dedup → forge.persist
review 流水线（docs/task-system.md §7（原 api-m3.md §3））：
    review.pair → review.debate → review.summarize

健壮性约定（与 actions_wiki 一致）：
- 逐 idea / 逐场辩论独立 try/except，单条失败不打断批处理，observation 汇总 failed；
- checkpoint 幂等：gap/候选/评分/去重结果与已完成对局都记入 checkpoint，
  断点续跑不重复调 LLM、persist 不重复入库；
- 判断性任务（gap 分析/生成/打分/辩论/裁判）走 core/llm 路由，其余全为确定性代码。
"""

import asyncio
import json
import re
import uuid
from datetime import timedelta
from itertools import zip_longest
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.actions import ActionContext, register
from app.core.db import get_sessionmaker
from app.core.embedding_space import EmbeddingSpace
from app.core.errors import PreconditionError
from app.core.llm.base import Message
from app.core.llm.budgets import FORGE_CONTEXT, FORGE_EXCERPT, resolve_budget
from app.models.activity import Activity
from app.models.base import utcnow
from app.models.idea import Idea
from app.models.library_direction import LibraryPaper
from app.models.paper import Concept, Paper, paper_concepts
from app.models.project import Project
from app.models.review import ReviewMessage, ReviewSession
from app.schemas.idea import FORGE_SIGNALS
from app.services import idea_dedup
from app.services.concepts import library_concept_ids
from app.services.embedding import embed_documents, upsert_idea_vector
from app.services.libraries import (
    dedupe_member_rows,
    get_source_library_ids,
    member_papers_stmt,
)
from app.services.review import (
    DEFAULT_PERSONAS,
    elo_update,
    human_comments,
    serialize_message,
)

DEFAULT_FORGE_KNOBS: dict[str, Any] = {
    "num_ideas": 8,
    "dedup_threshold": 0.85,
    "max_context_papers": 20,
    "signals": list(FORGE_SIGNALS),
}
DEFAULT_ROUNDS = 2

_SCORE_DIMS = ("novelty", "feasibility", "operability", "impact")

GAP_SYSTEM_PROMPT = """\
你是 Idea Forge 的研究空白分析师，基于项目知识库综述找出值得研究的空白。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"gaps": [{"title": "空白标题", "description": "为什么是空白、机会在哪"}]}
给出 3-6 个研究空白。
"""

LIMITATIONS_SYSTEM_PROMPT = """\
你是 Idea Forge 的文献局限分析师。下面给出若干论文中「局限/未来工作」相关段落摘录，
请归纳出其中反复出现或值得跟进的研究空白。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"gaps": [{"title": "空白标题", "description": "来自哪些论文的什么局限、机会在哪"}]}
给出 2-4 个研究空白。
"""

# 想法 / 研究方案各小节的写法。以前没有要求，模型常把几条并列内容挤成一大段
# 「……：1) …；2) …；3) …」，在想法页上是一整块密不透风的文字。
SECTION_STYLE_GUIDE = """\
各小节正文的写法（markdown）：
- 写短段落，每段只讲一件事，2-3 句为宜；不要把整节写成一大段；
- 有并列的几条内容（步骤、实验、风险、对比项）时用 markdown 列表，每条单独一行，
  以 "- " 或 "1. " 开头；不要在一段话里用 "1) … 2) …" 或 "(a) … (b) …" 连着写；
- 言简意赅：直接写做法和结论，不写套话和重复的铺垫。
"""

# 一次调用只写一个想法（POLARIS_FORGE_IDEA 是 fake provider 认的标记）。以前一次要
# 全部 num_ideas 个放在同一个对象里，八个想法的长 JSON 经常写到一半被截断。
GENERATE_SYSTEM_PROMPT = (
    """\
POLARIS_FORGE_IDEA
你是 Idea Forge 的想法生成器，围绕研究空白提出具体可执行的研究想法。
每次只写一个想法。用户消息会指明这一次针对研究空白清单里的哪一条，以及此前已经
写出的想法标题，生成时注意：
- 想法要针对指定的那条空白；
- 不要与已经写出的想法重复，换一个角度或路线。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"title": "想法标题", "summary": "一句话概述", "motivation": "动机",
"method": "方法概述", "experiments": "预期实验", "risks": "风险"}
motivation / method / experiments / risks 都是 markdown 字符串（换行写成 \\n），
每节不超过 150 字左右。
"""
    + SECTION_STYLE_GUIDE
)

SCORE_SYSTEM_PROMPT = """\
你是 Idea Forge 的评审员，对一个研究想法做四维独立打分（各维 0-10）。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"novelty": 0-10, "feasibility": 0-10, "operability": 0-10, "impact": 0-10,
 "rationale": {"novelty": "理由", "feasibility": "理由", "operability": "理由", "impact": "理由"}}
"""

JUDGE_SYSTEM_PROMPT = """\
你是评审人设「{name}」（立场：{stance}），担任这场科学辩论的裁判。
基于双方全部发言判定哪个想法更值得投入研究。
只输出一个 JSON 对象，不要输出任何其他文字，格式：
{{"winner": "a" 或 "b", "reason": "判定理由"}}
"""


# ---- 公共小件 ----


def _params(ctx: ActionContext) -> dict[str, Any]:
    params = (ctx.checkpoint or {}).get("params")
    return params if isinstance(params, dict) else {}


def resolve_forge_knobs(raw: Any) -> dict[str, Any]:
    knobs = dict(DEFAULT_FORGE_KNOBS)
    if isinstance(raw, dict):
        for key in DEFAULT_FORGE_KNOBS:
            if raw.get(key) is not None:
                knobs[key] = raw[key]
    return knobs


def _forge_knobs(ctx: ActionContext) -> dict[str, Any]:
    return resolve_forge_knobs(_params(ctx).get("knobs"))


async def _get_project(session: AsyncSession, ctx: ActionContext) -> Project:
    project = await session.get(Project, ctx.run.project_id)
    if project is None:
        raise ValueError(f"project not found: {ctx.run.project_id}")
    return project


def _statement(project: Project) -> str:
    return str(project.statement or project.name)


# ---- 各环节的输出 schema（结构化输出，不再从回答文字里切 JSON） ----

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class GapOut(BaseModel):
    title: NonEmptyStr
    description: str = ""


class GapsOut(BaseModel):
    """研究空白清单：综述空白分析与论文局限归纳共用。"""

    gaps: list[GapOut] = Field(min_length=1)


class ForgeIdeaOut(BaseModel):
    """一个候选想法（一次调用只要一个）。"""

    title: NonEmptyStr
    summary: str
    motivation: str
    method: str
    experiments: str
    risks: str


class ScoreRationaleOut(BaseModel):
    novelty: str
    feasibility: str
    operability: str
    impact: str


class IdeaScoreOut(BaseModel):
    """四维打分（各维 0-10）加逐维理由。"""

    novelty: int
    feasibility: int
    operability: int
    impact: int
    rationale: ScoreRationaleOut


class DebateVerdictOut(BaseModel):
    winner: Literal["a", "b"]
    reason: str = ""


async def _ask[T: BaseModel](
    ctx: ActionContext,
    *,
    stage: str,
    system: str,
    user: str,
    schema: type[T],
    cache_prefix: str | None = None,
) -> T:
    """一次结构化调用，返回校验过的 schema 实例。

    格式不对时的那一次重问由 ``complete_json`` 自己做；仍然不行、或者调用本身失败
    （网络、限流、密钥），抛带 ``code`` / ``transient`` 的 ``LLMError``，不在这里吞。
    ``cache_prefix`` 是同一步里多次调用共享的大段材料（知识库上下文）。
    """
    result = await ctx.llm.complete_json(
        stage,
        [
            Message(role="system", content=system + ctx.evidence_guidance()),
            Message(role="user", content=user),
        ],
        schema=schema,
        cache_prefix=cache_prefix,
        user_id=ctx.run.created_by,
        project_id=ctx.run.project_id,
        voyage_id=ctx.run.id,
    )
    return result.data


# 去重的算法在 services/idea_dedup.py（假设探索写想法时也用它）；这两个名字留在这里
# 是因为本模块和 actions_wiki 一直从这里取。
cosine_similarity = idea_dedup.cosine_similarity
_idea_text = idea_dedup.idea_text


def _idea_markdown(item: dict[str, Any]) -> str:
    sections = (
        ("动机", "motivation"),
        ("方法概述", "method"),
        ("预期实验", "experiments"),
        ("风险", "risks"),
    )
    parts = [f"## {zh}\n\n{str(item.get(key) or '（待补充）').strip()}" for zh, key in sections]
    return "\n\n".join(parts)


# ---- forge 1. 读取知识库上下文 ----

# 关联库里一篇可用的论文都没有时 forge.read_context 的失败码（前置条件，不是故障）
IDEA_CONTEXT_EMPTY = "IDEA_CONTEXT_EMPTY"
# 没有解读的论文在上下文里的标注
NO_SUMMARY_LABEL = "（这篇还没有解读，以下是原文摘要）"


def _context_body(paper: Paper) -> tuple[bool, str] | None:
    """一篇论文进上下文的正文：(有没有解读, 正文)；None = 这篇不可用。

    有解读用解读；没有就退到原文摘要；连摘要也没有但有 TL;DR 的，正文留空、靠
    TL;DR 那一行。三样都没有的论文只剩标题，给不了模型任何依据，不进上下文。
    """
    summary = (paper.wiki_content or "").strip()
    if summary:
        return True, summary
    abstract = (paper.abstract or "").strip()
    if abstract or (paper.tldr or "").strip():
        return False, abstract
    return None


@register("forge.read_context")
async def forge_read_context(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _forge_knobs(ctx)
    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        # 课题关联库并集：跨库同一论文按确定性视角归并，再按相关性截断
        library_ids = await get_source_library_ids(session, project.id)
        rows = (
            dedupe_member_rows(
                (
                    await session.execute(
                        # 不再 inner join 解读：手动添加 / Zotero / 浏览器插件进来的论文
                        # 是 included 但可能还没有解读，以前在这里整篇被丢掉（见下方兜底）
                        member_papers_stmt(library_ids).where(
                            LibraryPaper.status.in_(("compiled", "included"))
                        )
                    )
                ).all()
            )
            if library_ids
            else []
        )
        rows.sort(
            key=lambda pm: (
                -(pm[1].relevance_score if pm[1].relevance_score is not None else -1e18),
                pm[1].created_at,
            )
        )
        # 可用 = 有解读，或至少有摘要 / TL;DR（只有标题的论文给不了模型任何依据）
        rows = [pm for pm in rows if _context_body(pm[0]) is not None]
        rows = rows[: int(knobs["max_context_papers"])]
        papers = [p for p, _ in rows]
        if not papers:
            # 以前这里会把上下文写成「（知识库为空）」然后照常往下跑，模型只凭课题名
            # 编空白和想法，结果看起来和正常的一样。没有依据就不生成。
            raise PreconditionError(
                IDEA_CONTEXT_EMPTY,
                "关联的文献库里没有可用的论文，请先添加论文（至少要有摘要或解读）再生成想法",
            )
        concept_names = (
            (
                (
                    await session.execute(
                        select(Concept.name)
                        .where(Concept.id.in_(library_concept_ids(library_ids)))
                        .order_by(Concept.name)
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            if library_ids
            else []
        )

    # 两个预算都可以按环节调（设置 → 模型路由 → Idea Forge，#811）：单篇 wiki 摘录
    # 多长、整段知识库上下文总共多长。默认 800 / 12000，与改成可调之前一致。
    # 截断顺序：先按单篇截，再按总长截——总长截断会落在某一篇中间，排在后面（相关性
    # 更低）的论文先被挤掉。
    excerpt_chars = await resolve_budget(ctx.llm, FORGE_EXCERPT)
    context_chars = await resolve_budget(ctx.llm, FORGE_CONTEXT)
    parts = []
    without_summary = 0
    for paper in papers:
        has_summary, body = _context_body(paper) or (False, "")
        without_summary += not has_summary
        # 没有解读的论文用原文摘要顶上，并明说这不是解读——模型别把它当成读过全文
        label = "" if has_summary else f"{NO_SUMMARY_LABEL}\n"
        parts.append(
            f"### {paper.title}\nTL;DR：{paper.tldr or '（无）'}\n{label}{body[:excerpt_chars]}"
        )
    context_text = "\n\n".join(parts)[:context_chars]

    ctx.checkpoint["forge_context"] = {
        "paper_ids": [str(p.id) for p in papers],
        "concepts": list(concept_names),
        "text": context_text,
    }
    return {
        "papers": len(papers),
        "papers_without_summary": without_summary,
        "concepts": len(concept_names),
    }


def _context(ctx: ActionContext) -> dict[str, Any]:
    context = ctx.checkpoint.get("forge_context")
    return context if isinstance(context, dict) else {}


def _context_prompt(ctx: ActionContext, statement: str) -> str:
    context = _context(ctx)
    concepts = "、".join(context.get("concepts") or []) or "（无）"
    return (
        f"研究方向：{statement}\n"
        f"知识库概念：{concepts}\n"
        f"知识库综述（compiled wiki 摘要）：\n{context.get('text') or '（知识库为空）'}"
    )


# ---- forge 2. 信号采集（确定性优先，docs/task-system.md §7（原 api-idea2.md §1）） ----

_TREND_WINDOW_DAYS = 90
_HOLE_TOP_CONCEPTS = 8  # 每组参与配对的高频概念数
_HOLE_MAX_PAIRS = 5
_TREND_MAX = 5
_LIMIT_KEYWORDS = ("limitation", "future work", "future direction", "局限", "未来工作", "不足")
_LIMIT_PAPERS = 8  # 参与局限抽取的论文数上限
_LIMIT_EXCERPT_CHARS = 600
# method 系类别 ×（problem 类）做组合空白挖掘
_HOLE_METHOD_CATEGORIES = ("method", "architecture", "methodology")


def _signals_enabled(ctx: ActionContext) -> list[str]:
    raw = _forge_knobs(ctx).get("signals")
    if not isinstance(raw, list):
        return list(FORGE_SIGNALS)
    enabled = [s for s in raw if s in FORGE_SIGNALS]
    return enabled or list(FORGE_SIGNALS)


async def _concept_paper_map(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[uuid.UUID, tuple[str, str, set[uuid.UUID]]]:
    """概念 → (name, category, 关联论文集合)。"""
    library_ids = await get_source_library_ids(session, project_id)
    if not library_ids:
        return {}
    rows = (
        await session.execute(
            select(Concept.id, Concept.name, Concept.category, paper_concepts.c.paper_id)
            .join(paper_concepts, paper_concepts.c.concept_id == Concept.id)
            .where(Concept.id.in_(library_concept_ids(library_ids)))
        )
    ).all()
    result: dict[uuid.UUID, tuple[str, str, set[uuid.UUID]]] = {}
    for concept_id, name, category, paper_id in rows:
        entry = result.setdefault(concept_id, (str(name), str(category or "other"), set()))
        entry[2].add(paper_id)
    return result


def _concept_holes(
    concept_map: dict[uuid.UUID, tuple[str, str, set[uuid.UUID]]],
) -> list[dict[str, Any]]:
    """method 系 × problem 类中各自高频但零共现的概念对（纯代码，无 LLM）。"""
    methods = sorted(
        (v for v in concept_map.values() if v[1] in _HOLE_METHOD_CATEGORIES),
        key=lambda v: -len(v[2]),
    )[:_HOLE_TOP_CONCEPTS]
    problems = sorted(
        (v for v in concept_map.values() if v[1] == "problem"),
        key=lambda v: -len(v[2]),
    )[:_HOLE_TOP_CONCEPTS]
    pairs: list[tuple[int, dict[str, Any]]] = []
    for m_name, _, m_papers in methods:
        for p_name, _, p_papers in problems:
            if m_papers & p_papers:
                continue
            coverage = len(m_papers) + len(p_papers)
            pairs.append(
                (
                    coverage,
                    {
                        "method": m_name,
                        "problem": p_name,
                        "method_papers": len(m_papers),
                        "problem_papers": len(p_papers),
                    },
                )
            )
    pairs.sort(key=lambda x: -x[0])
    return [p for _, p in pairs[:_HOLE_MAX_PAIRS]]


async def _trend_concepts(session: AsyncSession, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """近 90 天入库论文中的高频概念（纯代码，无 LLM）。"""
    cutoff = utcnow() - timedelta(days=_TREND_WINDOW_DAYS)
    library_ids = await get_source_library_ids(session, project_id)
    if not library_ids:
        return []
    rows = (
        await session.execute(
            select(Concept.name, Paper.id)
            .join(paper_concepts, paper_concepts.c.concept_id == Concept.id)
            .join(Paper, Paper.id == paper_concepts.c.paper_id)
            .join(
                LibraryPaper,
                (LibraryPaper.paper_id == Paper.id) & (LibraryPaper.library_id.in_(library_ids)),
            )
            .where(
                Concept.id.in_(library_concept_ids(library_ids)),
                LibraryPaper.created_at >= cutoff,
            )
        )
    ).all()
    # 跨库并集：同一 (概念名, 论文) 只计一次（论文可能在多个关联库、概念各库一份）
    counts: dict[str, int] = {}
    seen: set[tuple[str, uuid.UUID]] = set()
    for name, paper_id in rows:
        if (str(name), paper_id) in seen:
            continue
        seen.add((str(name), paper_id))
        counts[str(name)] = counts.get(str(name), 0) + 1
    top = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:_TREND_MAX]
    return [{"concept": name, "recent_papers": n} for name, n in top if n >= 2]


def _limitation_excerpts(full_text: str) -> list[str]:
    """定位「局限/未来工作」相关段落（标题/关键词启发式），最多 2 段。"""
    excerpts: list[str] = []
    for para in re.split(r"\n\s*\n", full_text):
        stripped = para.strip()
        if len(stripped) < 60:
            continue
        lowered = stripped.lower()
        if any(k in lowered for k in _LIMIT_KEYWORDS):
            excerpts.append(stripped[:_LIMIT_EXCERPT_CHARS])
            if len(excerpts) >= 2:
                break
    return excerpts


@register("forge.collect_signals")
async def forge_collect_signals(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """确定性信号采集：概念组合空白 / 时间趋势 / 论文局限（LLM 仅做局限归纳）。"""
    if isinstance(ctx.checkpoint.get("forge_signals"), dict):  # 断点幂等
        signals = ctx.checkpoint["forge_signals"]
        return {"skipped": True, **{k: len(v) for k, v in signals.items() if isinstance(v, list)}}
    enabled = _signals_enabled(ctx)
    signals: dict[str, Any] = {}

    async with get_sessionmaker()() as session:
        if "concept_holes" in enabled:
            concept_map = await _concept_paper_map(session, ctx.run.project_id)
            signals["concept_holes"] = _concept_holes(concept_map)
        if "trends" in enabled:
            signals["trends"] = await _trend_concepts(session, ctx.run.project_id)
        excerpt_blocks: list[str] = []
        if "limitations" in enabled:
            library_ids = await get_source_library_ids(session, ctx.run.project_id)
            limit_rows = (
                dedupe_member_rows(
                    (
                        await session.execute(
                            member_papers_stmt(library_ids).where(
                                Paper.full_text_path.is_not(None),
                                LibraryPaper.status.in_(("compiled", "included")),
                            )
                        )
                    ).all()
                )
                if library_ids
                else []
            )
            limit_rows.sort(
                key=lambda pm: (
                    -(pm[1].relevance_score if pm[1].relevance_score is not None else -1e18),
                    pm[1].created_at,
                )
            )
            papers = [p for p, _ in limit_rows[:_LIMIT_PAPERS]]
            for paper in papers:
                path = Path(paper.full_text_path or "")
                if not path.is_file():
                    continue
                excerpts = _limitation_excerpts(path.read_text(encoding="utf-8", errors="ignore"))
                if excerpts:
                    excerpt_blocks.append(f"### {paper.title}\n" + "\n".join(excerpts))

    if "limitations" in enabled:
        if excerpt_blocks:
            try:
                found = await _ask(
                    ctx,
                    stage="forge_signal",
                    system=LIMITATIONS_SYSTEM_PROMPT,
                    user="\n\n".join(excerpt_blocks)[:12000],
                    schema=GapsOut,
                )
                signals["limitations"] = [
                    {"title": g.title, "description": g.description} for g in found.gaps
                ]
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 单信号失败不阻塞其余信号
                signals["limitations"] = []
                signals["limitations_error"] = f"{type(e).__name__}: {e}"
        else:
            signals["limitations"] = []

    ctx.checkpoint["forge_signals"] = signals
    return {
        "enabled": enabled,
        **{k: len(v) for k, v in signals.items() if isinstance(v, list)},
    }


# ---- forge 3. gap 综合（LLM 综述空白 + 确定性信号合并，stage=forge） ----


def _interleave_by_signal(buckets: dict[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    """按信号轮转铺开 gap 清单，而不是一个信号接一个信号地拼（#428）。

    生成器只写 num_ideas（默认 8）条想法，而 gap 清单常有 20+ 条。顺序拼接的
    后果是排在后面的信号整段落在清单尾部——新加的信号无论质量多好，都大概率
    根本轮不到。轮转之后每个信号的头几条都靠前，信号之间按「各自最好的那条」
    竞争，而不是按谁先被拼进来。
    """
    ordered = [items for items in buckets.values() if items]
    out: list[dict[str, str]] = []
    for row in zip_longest(*ordered):
        out.extend(item for item in row if item is not None)
    return out


@register("forge.gap_analysis")
async def forge_gap_analysis(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("forge_gaps"), list):  # 断点幂等
        return {"gaps": len(ctx.checkpoint["forge_gaps"]), "skipped": True}
    enabled = _signals_enabled(ctx)

    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        statement = _statement(project)

    # 按信号分桶收集，最后轮转铺开（#428）；插入顺序即轮转顺序，保持确定性
    buckets: dict[str, list[dict[str, str]]] = {}
    if "survey_gap" in enabled:
        found = await _ask(
            ctx,
            stage="forge",
            system=GAP_SYSTEM_PROMPT + ctx.workflow_guidance("forge.gap_analysis"),
            user=_context_prompt(ctx, statement),
            schema=GapsOut,
        )
        buckets["survey_gap"] = [
            {"title": g.title, "description": g.description, "signal": "survey_gap"}
            for g in found.gaps
        ]

    # 确定性信号 → gap 条目（不经 LLM）
    signals = ctx.checkpoint.get("forge_signals") or {}
    for hole in signals.get("concept_holes") or []:
        buckets.setdefault("concept_holes", []).append(
            {
                "title": f"概念组合空白：{hole['method']} × {hole['problem']}",
                "description": (
                    f"「{hole['method']}」（{hole['method_papers']} 篇）与"
                    f"「{hole['problem']}」（{hole['problem_papers']} 篇）在库内零共现，"
                    "两者的结合尚无人探索"
                ),
                "signal": "concept_holes",
            }
        )
    for trend in signals.get("trends") or []:
        buckets.setdefault("trends", []).append(
            {
                "title": f"新兴主题：{trend['concept']}",
                "description": (
                    f"近 {_TREND_WINDOW_DAYS} 天有 {trend['recent_papers']} 篇新论文涉及"
                    f"「{trend['concept']}」，处于快速演化期，存在跟进机会"
                ),
                "signal": "trends",
            }
        )
    for gap in signals.get("limitations") or []:
        buckets.setdefault("limitations", []).append({**gap, "signal": "limitations"})

    gaps = _interleave_by_signal(buckets)
    if not gaps:
        raise ValueError("没有可用的研究空白（所有信号源均为空）")
    ctx.checkpoint["forge_gaps"] = gaps
    return {
        "gaps": len(gaps),
        "by_signal": {
            s: sum(1 for g in gaps if g.get("signal") == s) for s in {g.get("signal") for g in gaps}
        },
        "titles": [g["title"] for g in gaps],
    }


# ---- forge 3. 生成候选（LLM stage=forge） ----


@register("forge.generate")
async def forge_generate(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("forge_candidates"), list):  # 断点幂等
        return {"generated": len(ctx.checkpoint["forge_candidates"]), "skipped": True}
    knobs = _forge_knobs(ctx)
    num_ideas = int(knobs["num_ideas"])

    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        statement = _statement(project)

    gaps = ctx.checkpoint.get("forge_gaps") or []
    # 每个想法各问一次；知识库上下文与空白清单每次都一样，放进可缓存的前缀
    gap_list = json.dumps(gaps, ensure_ascii=False)
    shared = f"{_context_prompt(ctx, statement)}\n研究空白清单：{gap_list}"
    system = GENERATE_SYSTEM_PROMPT + ctx.workflow_guidance("forge.generate")
    # 写到一半失败（网络、限流）再续跑时，已经写好的那几个不重写
    partial = ctx.checkpoint.get("forge_candidates_partial")
    candidates: list[dict[str, Any]] = list(partial) if isinstance(partial, list) else []
    for i in range(len(candidates), num_ideas):
        # 空白清单已按信号轮转铺开（_interleave_by_signal），想法依次各领一条，
        # 不同想法自然落在不同空白、不同信号上——以前这件事交给模型自己选下标
        index = i % len(gaps) if gaps else None
        gap = gaps[index] if index is not None else None
        target = (
            f"这一次针对清单里下标 {index} 的空白：{gap.get('title') or ''}\n"
            if gap is not None
            else ""
        )
        earlier = json.dumps([c["title"] for c in candidates], ensure_ascii=False)
        idea = await _ask(
            ctx,
            stage="forge_generate",
            system=system,
            user=(
                f"{target}此前已经写出的想法标题：{earlier}\n"
                f"请生成第 {i + 1} 个候选想法（共 {num_ideas} 个）。"
            ),
            schema=ForgeIdeaOut,
            cache_prefix=shared,
        )
        # evidence 记录这个想法依据的是哪条空白、来自哪个信号（docs/task-system.md §7）
        evidence = (
            [
                {
                    "source": "signal",
                    "title": str(gap.get("title") or ""),
                    "why": str(gap.get("description") or ""),
                    "signal": str(gap.get("signal") or "survey_gap"),
                }
            ]
            if gap is not None
            else None
        )
        candidates.append(
            {
                "title": idea.title[:512],
                "summary": idea.summary.strip() or None,
                "content": _idea_markdown(idea.model_dump()),
                "scores": None,
                "score_rationale": None,
                "duplicate": False,
                "evidence": evidence,
            }
        )
        ctx.checkpoint["forge_candidates_partial"] = list(candidates)
    ctx.checkpoint["forge_candidates"] = candidates
    ctx.checkpoint.pop("forge_candidates_partial", None)
    return {"requested": num_ideas, "generated": len(candidates)}


# ---- forge 4. 四维打分（LLM stage=forge，逐 idea 独立调用） ----


def _scores_of(out: IdeaScoreOut) -> tuple[dict[str, float], dict[str, str]]:
    """打分结果 → (四维分数，夹到 0-10；逐维理由)。"""
    scores = {dim: min(10.0, max(0.0, float(getattr(out, dim)))) for dim in _SCORE_DIMS}
    rationale = {dim: str(getattr(out.rationale, dim) or "") for dim in _SCORE_DIMS}
    return scores, rationale


@register("forge.score")
async def forge_score(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = list(ctx.checkpoint.get("forge_candidates") or [])
    scored = 0
    skipped = 0
    failed: list[dict[str, str]] = []

    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        statement = _statement(project)

    for cand in candidates:
        if isinstance(cand.get("scores"), dict):  # 断点幂等：已评分不重复调 LLM
            skipped += 1
            continue
        user_prompt = (
            f"研究方向：{statement}\n"
            f"想法标题：{cand['title']}\n"
            f"一句话概述：{cand.get('summary') or '（无）'}\n"
            f"详情：\n{cand.get('content') or ''}"
        )
        try:
            scores, rationale = _scores_of(
                await _ask(
                    ctx,
                    stage="forge",
                    system=SCORE_SYSTEM_PROMPT + ctx.workflow_guidance("forge.score"),
                    user=user_prompt,
                    schema=IdeaScoreOut,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 逐 idea 失败隔离（无评分入库）
            failed.append({"title": str(cand["title"]), "error": f"{type(e).__name__}: {e}"})
            continue
        cand["scores"] = scores
        cand["score_rationale"] = rationale
        scored += 1

    ctx.checkpoint["forge_candidates"] = candidates
    return {
        "processed": len(candidates),
        "succeeded": scored,
        "skipped": skipped,
        "failed": failed,
    }


# ---- forge 5. 语义去重（embedding 余弦 + rerank 复核） ----


@register("forge.dedup")
async def forge_dedup(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _forge_knobs(ctx)
    threshold = float(knobs["dedup_threshold"])
    candidates: list[dict[str, Any]] = list(ctx.checkpoint.get("forge_candidates") or [])
    if ctx.checkpoint.get("forge_dedup_done"):  # 断点幂等
        dropped = [c for c in candidates if c.get("duplicate")]
        return {"candidates": len(candidates), "dropped": len(dropped), "skipped": True}
    if not candidates:
        ctx.checkpoint["forge_dedup_done"] = True
        return {"candidates": 0, "dropped": 0}

    cand_texts = [_idea_text(c["title"], c.get("summary")) for c in candidates]
    # 候选与库内既有想法的向量必须出自同一个模型，否则下面的阈值判定毫无意义——
    # 所以两批都走统一入口，并且只跟激活空间下的既有向量比。
    async with get_sessionmaker()() as session:
        try:
            cand_vectors, space = await embed_documents(
                session,
                cand_texts,
                user_id=ctx.run.created_by,
                project_id=ctx.run.project_id,
                voyage_id=ctx.run.id,
            )
        except NotImplementedError:
            # embedding 路由不可用：跳过去重（记录原因），全部候选放行
            ctx.checkpoint["forge_dedup_done"] = True
            return {"candidates": len(candidates), "dropped": 0, "skipped_reason": "no embedding"}

        # 库内既有 idea（未淘汰、不在回收站）：激活空间下无向量的现场补嵌并落库
        existing_entries = await idea_dedup.existing_idea_entries(
            session,
            project_id=ctx.run.project_id,
            space=space,
            user_id=ctx.run.created_by,
            voyage_id=ctx.run.id,
        )

    dropped: list[dict[str, Any]] = []
    kept: list[idea_dedup.DedupEntry] = list(existing_entries)
    for cand, text, vector in zip(candidates, cand_texts, cand_vectors, strict=True):
        match = await idea_dedup.find_duplicate(
            ctx.llm,
            text=text,
            vector=vector,
            kept=kept,
            threshold=threshold,
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
        )
        if match is not None:
            dropped.append(
                {
                    "title": cand["title"],
                    "duplicate_of": match.entry.label,
                    "cosine": round(match.cosine, 4),
                    "rerank_score": round(match.rerank_score, 4),
                }
            )
        else:
            kept.append(
                idea_dedup.DedupEntry(
                    label=f"本批候选「{cand['title']}」", text=text, vector=vector
                )
            )
        cand["duplicate"] = match is not None

    ctx.checkpoint["forge_candidates"] = candidates
    ctx.checkpoint["forge_dedup_done"] = True
    return {
        "candidates": len(candidates),
        "existing_compared": len(existing_entries),
        "dropped": len(dropped),
        "dropped_detail": dropped,
        "threshold": threshold,
    }


# ---- forge 6. 入库候选池 ----


@register("forge.persist")
async def forge_persist(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("forge_inserted_ids"), list):  # 断点幂等：不重复入库
        return {"inserted": len(ctx.checkpoint["forge_inserted_ids"]), "skipped": True}
    candidates: list[dict[str, Any]] = list(ctx.checkpoint.get("forge_candidates") or [])
    survivors = [c for c in candidates if not c.get("duplicate")]
    parent_paper_ids = list(_context(ctx).get("paper_ids") or [])

    inserted_ids: list[str] = []
    async with get_sessionmaker()() as session:
        embeddings: list[list[float] | None] = [None] * len(survivors)
        space: EmbeddingSpace | None = None
        if survivors:
            try:
                embeddings, space = await embed_documents(
                    session,
                    [_idea_text(c["title"], c.get("summary")) for c in survivors],
                    user_id=ctx.run.created_by,
                    project_id=ctx.run.project_id,
                    voyage_id=ctx.run.id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 嵌入失败不阻塞入库（含 NotImplementedError）
                embeddings, space = [None] * len(survivors), None

        for cand, vector in zip(survivors, embeddings, strict=True):
            idea = Idea(
                project_id=ctx.run.project_id,
                title=cand["title"],
                summary=cand.get("summary"),
                content=cand.get("content"),
                scores=cand.get("scores"),
                score_rationale=cand.get("score_rationale"),
                status="candidate",
                depth="sketch",
                evidence=cand.get("evidence"),
                parent_paper_ids=parent_paper_ids,
            )
            session.add(idea)
            await session.flush()
            if vector is not None and space is not None:
                await upsert_idea_vector(session, idea.id, vector, space)
            inserted_ids.append(str(idea.id))
        session.add(
            Activity(
                project_id=ctx.run.project_id,
                actor="agent:idea-forge",
                kind="forge.completed",
                message=f"Idea Forge 完成：{len(inserted_ids)} 个候选想法入库",
                payload={
                    "voyage_id": str(ctx.run.id),
                    "inserted": len(inserted_ids),
                    "dropped_duplicates": len(candidates) - len(survivors),
                },
            )
        )
        await session.commit()

    ctx.checkpoint["forge_inserted_ids"] = inserted_ids
    return {
        "inserted": len(inserted_ids),
        "dropped_duplicates": len(candidates) - len(survivors),
        "idea_ids": inserted_ids,
    }


# ---- review 1. 配对（Swiss：按 Elo 排序相邻配对） ----


def _personas(ctx: ActionContext) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """人设分工：[0]=正方 [1]=反方 [2]=裁判；不足三个用默认人设补齐。

    优先级：显式 params.personas > persona 技能（review.debate）> 内置默认。
    """
    # 技能移除后没有第二处人设来源；参数没给就用下面的内置默认
    raw = _params(ctx).get("personas")
    personas = [p for p in raw if isinstance(p, dict) and p.get("name")] if raw else []
    merged = (personas + DEFAULT_PERSONAS[len(personas) :])[:3]
    return merged[0], merged[1], merged[2]


def _rounds(ctx: ActionContext) -> int:
    try:
        return max(1, min(5, int(_params(ctx).get("rounds") or DEFAULT_ROUNDS)))
    except (TypeError, ValueError):
        return DEFAULT_ROUNDS


def _pair_key(idea_a: str, idea_b: str) -> tuple[str, str]:
    return (idea_a, idea_b) if idea_a < idea_b else (idea_b, idea_a)


@register("review.pair")
async def review_pair(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("review_pairs"), list):  # 断点幂等
        return {"pairs": len(ctx.checkpoint["review_pairs"]), "skipped": True}
    explicit_ids = _params(ctx).get("idea_ids")
    retry_pairs = _params(ctx).get("retry_pairs")

    async with get_sessionmaker()() as session:
        if isinstance(retry_pairs, list):
            pairs = [
                [str(pair[0]), str(pair[1])]
                for pair in retry_pairs
                if isinstance(pair, list) and len(pair) == 2
            ]
            participant_ids = {idea_id for pair in pairs for idea_id in pair}
            ideas = list(
                (
                    await session.execute(
                        select(Idea).where(
                            Idea.project_id == ctx.run.project_id,
                            Idea.id.in_([uuid.UUID(i) for i in participant_ids]),
                            Idea.trashed_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if {str(i.id) for i in ideas} != participant_ids:
                raise ValueError("retry participant missing or trashed")
            ctx.checkpoint["review_pairs"] = pairs
            return {
                "participants": len(participant_ids),
                "pairs": len(pairs),
                "bye": None,
                "retry": True,
                "plan_signal": {"decision": "matches", "count": len(pairs)},
            }

        stmt = select(Idea).where(
            Idea.project_id == ctx.run.project_id,
            Idea.trashed_at.is_(None),
        )
        if explicit_ids:
            stmt = stmt.where(Idea.id.in_([uuid.UUID(str(i)) for i in explicit_ids]))
        else:
            stmt = stmt.where(Idea.status.in_(("candidate", "under_review")))
        stmt = stmt.order_by(Idea.elo_rating.desc(), Idea.created_at)
        ideas = (await session.execute(stmt)).scalars().all()

        status_changed: list[Idea] = []
        for idea in ideas:
            if idea.status == "candidate":
                idea.status = "under_review"
                status_changed.append(idea)
        await session.commit()
        for idea in status_changed:
            await ctx.notify(
                {"type": "idea.status", "idea_id": str(idea.id), "status": idea.status}
            )
        ordered = [(str(i.id), i.depth) for i in ideas]

    # 同 depth 才配对（sketch 对 sketch、proposal 对 proposal，docs/task-system.md §7）
    pairs: list[list[str]] = []
    byes: list[str] = []
    for depth in ("proposal", "sketch"):
        group = [idea_id for idea_id, d in ordered if d == depth]
        pairs.extend([group[i], group[i + 1]] for i in range(0, len(group) - 1, 2))
        if len(group) % 2 == 1:
            byes.append(group[-1])
    ctx.checkpoint["review_pairs"] = pairs
    # 配对数决定对局数：信号表据此把辩论展开成 N 个 review.match 节点（引擎可逐场查预算）
    return {
        "participants": len(ordered),
        "pairs": len(pairs),
        "bye": byes or None,
        "plan_signal": {"decision": "matches", "count": len(pairs)},
    }


# ---- review 2. 科学辩论 + 裁判判定 + Elo 更新 ----


def _persona_system(persona: dict[str, str], side: str, idea_title: str) -> str:
    return (
        f"你是评审人设「{persona['name']}」，立场：{persona.get('stance') or ''}。\n"
        f"你在一场关于两个研究想法的科学辩论中担任{side}，为想法「{idea_title}」辩护，"
        "同时指出对方想法的不足。\n"
        "请用中文发言，观点犀利、有理有据，直接输出发言内容。"
    )


def _debate_brief(label: str, idea: Idea) -> str:
    # proposal 型内容厚（Research Proposal），截断放宽（docs/task-system.md §7）
    limit = 4000 if idea.depth == "proposal" else 2000
    return (
        f"想法 {label}：{idea.title}\n"
        f"概述：{idea.summary or '（无）'}\n"
        f"详情：\n{(idea.content or '')[:limit]}"
    )


def _human_block(label: str, comments: list[str]) -> str:
    if not comments:
        return f"想法 {label} 的人类评审意见：（暂无）"
    return f"想法 {label} 的人类评审意见：\n" + "\n".join(f"- {c}" for c in comments)


async def _add_agent_message(
    session: AsyncSession,
    ctx: ActionContext,
    review_session: ReviewSession,
    *,
    author_name: str,
    content: str,
    round_no: int,
) -> ReviewMessage:
    message = ReviewMessage(
        session_id=review_session.id,
        author_type="agent",
        author_name=author_name,
        content=content,
        round=round_no,
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)
    await ctx.notify(
        {
            "type": "review.message",
            "session_id": str(review_session.id),
            "project_id": str(ctx.run.project_id),
            "message": serialize_message(message),
        }
    )
    return message


async def _run_match(
    ctx: ActionContext, session: AsyncSession, idea_a: Idea, idea_b: Idea, match_no: int
) -> dict[str, Any]:
    pro, con, judge = _personas(ctx)
    rounds = _rounds(ctx)

    review_session = ReviewSession(
        target_type="idea_match",
        target_id=idea_a.id,
        payload={"idea_a": str(idea_a.id), "idea_b": str(idea_b.id), "round": match_no},
    )
    session.add(review_session)
    await session.commit()
    await session.refresh(review_session)

    comments_a = await human_comments(session, idea_a.id)
    comments_b = await human_comments(session, idea_b.id)
    base_context = (
        f"{_debate_brief('A', idea_a)}\n\n{_debate_brief('B', idea_b)}\n\n"
        f"{_human_block('A', comments_a)}\n{_human_block('B', comments_b)}"
    )

    transcript: list[str] = []
    round_no = 0
    for debate_round in range(1, rounds + 1):
        for persona, side, idea in ((pro, "正方", idea_a), (con, "反方", idea_b)):
            round_no += 1
            transcript_text = "\n\n".join(transcript) or "（辩论刚开始）"
            user_prompt = (
                f"{base_context}\n\n此前发言：\n{transcript_text}\n\n"
                f"第 {debate_round} 轮，请发表你的{side}观点。"
            )
            result = await ctx.llm.complete(
                "debate",
                [
                    Message(
                        role="system",
                        content=_persona_system(persona, side, idea.title)
                        + ctx.evidence_guidance(),
                    ),
                    Message(role="user", content=user_prompt),
                ],
                user_id=ctx.run.created_by,
                project_id=ctx.run.project_id,
                voyage_id=ctx.run.id,
            )
            content = result.content.strip() or "（无发言）"
            await _add_agent_message(
                session,
                ctx,
                review_session,
                author_name=str(persona["name"]),
                content=content,
                round_no=round_no,
            )
            transcript.append(f"{persona['name']}（{side}）：{content}")

    # 裁判判定（结构化输出）
    judged = await _ask(
        ctx,
        stage="debate",
        system=JUDGE_SYSTEM_PROMPT.format(name=judge["name"], stance=judge.get("stance") or ""),
        user=f"{base_context}\n\n辩论全文：\n" + "\n\n".join(transcript),
        schema=DebateVerdictOut,
    )
    verdict = {"winner": judged.winner, "reason": judged.reason}
    round_no += 1
    winner_idea = idea_a if verdict["winner"] == "a" else idea_b
    await _add_agent_message(
        session,
        ctx,
        review_session,
        author_name=str(judge["name"]),
        content=f"判定胜者：{verdict['winner']}（{winner_idea.title}）。理由:{verdict['reason']}",
        round_no=round_no,
    )

    # Elo 更新（K=32）+ 战绩累计
    new_a, new_b = elo_update(idea_a.elo_rating, idea_b.elo_rating, verdict["winner"])
    idea_a.elo_rating = new_a
    idea_b.elo_rating = new_b
    idea_a.matches += 1
    idea_b.matches += 1
    winner_idea.wins += 1
    review_session.payload = dict(review_session.payload or {}) | {
        "winner": verdict["winner"],
        "reason": verdict["reason"],
    }
    review_session.status = "closed"
    await session.commit()

    return {
        "session_id": str(review_session.id),
        "idea_a": str(idea_a.id),
        "idea_b": str(idea_b.id),
        "winner": verdict["winner"],
        "elo": {str(idea_a.id): round(new_a, 2), str(idea_b.id): round(new_b, 2)},
    }


@register("review.debate")
async def review_debate(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """已废弃：新计划用 review.match 逐场展开（引擎可逐场查预算）。仅为迁移前
    在途 idea_review 断点续跑保留（它们的计划里仍有 review.debate 节点）。"""
    pairs: list[list[str]] = list(ctx.checkpoint.get("review_pairs") or [])
    results: list[dict[str, Any]] = list(ctx.checkpoint.get("review_results") or [])
    done = {(r["idea_a"], r["idea_b"]) for r in results}
    failed: list[dict[str, str]] = []

    async with get_sessionmaker()() as session:
        for match_no, pair in enumerate(pairs, start=1):
            key = (str(pair[0]), str(pair[1]))
            if key in done:  # 断点幂等：已完成对局不重赛
                continue
            try:
                idea_a = await session.get(Idea, uuid.UUID(str(pair[0])))
                idea_b = await session.get(Idea, uuid.UUID(str(pair[1])))
                if idea_a is None or idea_b is None:
                    raise ValueError("idea not found")
                result = await _run_match(ctx, session, idea_a, idea_b, match_no)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 单场辩论失败隔离
                failed.append(
                    {"pair": f"{pair[0]} vs {pair[1]}", "error": f"{type(e).__name__}: {e}"}
                )
                continue
            results.append(result)
            ctx.checkpoint["review_results"] = results

    ctx.checkpoint["review_results"] = results
    return {"matches": len(pairs), "completed": len(results), "failed": failed}


@register("review.match")
async def review_match(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """单场辩论（review.pair 后由信号表按对局数展开成 N 个 review.match 节点）。

    炸开单体 debate 的收益：每场对局是可见节点，引擎能在对局之间查预算、超限走降级
    收尾（docs/task-system.md §7）；单场失败隔离（记 failed，不炸锦标赛）；
    断点幂等按 pair key（已跑过的对局不重赛）。
    """
    idx = int(params.get("match_index", 0))
    pairs: list[list[str]] = list(ctx.checkpoint.get("review_pairs") or [])
    if idx >= len(pairs):
        return {"match_index": idx, "skipped": True}
    pair = pairs[idx]
    results: list[dict[str, Any]] = list(ctx.checkpoint.get("review_results") or [])
    if any(r["idea_a"] == str(pair[0]) and r["idea_b"] == str(pair[1]) for r in results):
        return {"match_index": idx, "skipped": True}  # 断点幂等

    async with get_sessionmaker()() as session:
        try:
            idea_a = await session.get(Idea, uuid.UUID(str(pair[0])))
            idea_b = await session.get(Idea, uuid.UUID(str(pair[1])))
            if idea_a is None or idea_b is None:
                raise ValueError("idea not found")
            result = await _run_match(ctx, session, idea_a, idea_b, idx + 1)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 单场辩论失败隔离，不炸锦标赛
            return {"match_index": idx, "failed": f"{type(e).__name__}: {e}"}

    results.append(result)
    ctx.checkpoint["review_results"] = results
    return {"match_index": idx, "winner": result["winner"], "elo": result["elo"]}


# ---- review 3. 汇总 ----


@register("review.summarize")
async def review_summarize(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = list(ctx.checkpoint.get("review_results") or [])
    planned_pairs: list[list[str]] = list(ctx.checkpoint.get("review_pairs") or [])
    completed = {
        _pair_key(str(result["idea_a"]), str(result["idea_b"]))
        for result in results
        if result.get("idea_a") and result.get("idea_b")
    }
    failed_pairs = [
        pair for pair in planned_pairs if _pair_key(str(pair[0]), str(pair[1])) not in completed
    ]
    ctx.checkpoint["review_failed_pairs"] = failed_pairs
    async with get_sessionmaker()() as session:
        session.add(
            Activity(
                project_id=ctx.run.project_id,
                actor="agent:idea-review",
                kind="review.completed",
                message=(
                    f"Idea 评审锦标赛完成：{len(results)}/{len(planned_pairs)} 场成功"
                    if failed_pairs
                    else f"Idea 评审锦标赛完成：{len(results)} 场辩论"
                ),
                payload={
                    "voyage_id": str(ctx.run.id),
                    "matches": len(results),
                    "planned": len(planned_pairs),
                    "failed": len(failed_pairs),
                },
            )
        )
        await session.commit()
    return {
        "matches": len(results),
        "planned": len(planned_pairs),
        "failed": len(failed_pairs),
        "partial": bool(failed_pairs),
        "failed_pairs": failed_pairs,
        "results": results,
    }
