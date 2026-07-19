"""idea_proposal 动作集（docs/api-idea2.md）：目标构建 → 方案深耕 → 评审修订。

流水线（navigator.proposal_plan）：
    goal.explore →（idea_goal 闸门）goal.refine → proposal.related_work →
    proposal.design → proposal.experiments → proposal.novelty_check →
    proposal.risks → proposal.assemble → proposal.review_revise

约定：
- 判断性任务（探索决策/各节起草/新颖性论证/评审修订）走 core/llm 路由；
  检索、goal 结构校验、覆盖率检查等确定性逻辑全为普通代码；
- 每个动作用 observation["self_check"] 携带机械验收结论，Sextant 直接采信
  （不再调 LLM 复判）；验收不过 → 引擎带诊断走确定性重规划（navigator.proposal_replan）；
- novelty_check 三档判定：novel 通过；NEEDS_DIFFERENTIATION → 回炉 design；
  DUPLICATE → idea_pivot 闸门（人工决定调整方向/终止）；
- checkpoint 幂等：goal / proposal_sections / idea_id 均记入 checkpoint，断点续跑不重复。
"""

import asyncio
import json
import uuid
from typing import Any

from sqlalchemy import select

from app.agents.voyage import lit_tools
from app.agents.voyage.actions import ActionContext, register
from app.agents.voyage.actions_ideas import (
    SCORE_SYSTEM_PROMPT,
    _complete_json,
    _get_project,
    _params,
    _statement,
    _validate_scores,
    cosine_similarity,
)
from app.agents.voyage.tool_loop import run_tool_loop
from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.models.activity import Activity
from app.models.gate import Gate
from app.models.idea import RESEARCH_TYPES, Idea
from app.models.paper import Paper
from app.models.review import ReviewMessage, ReviewSession
from app.services.literature.openalex import OpenAlexClient
from app.services.literature.semantic_scholar import SemanticScholarClient
from app.services.review import serialize_message

DEFAULT_DEEP_KNOBS: dict[str, Any] = {
    "confirm_goal": True,
    "max_tool_calls": 15,
    "external_search": True,
    "revise_rounds": 2,
    "budget_tokens": None,
}

_SECTION_CONTEXT_CHARS = 10000  # 各节起草上下文截断
_PROPOSAL_REVIEW_CHARS = 9000  # 评审时 proposal 全文截断
_GROUNDING_EXCERPT_CHARS = 700  # grounding 论文 wiki 摘录
_INTERNAL_SIMILAR_K = 5
_EXTERNAL_SIMILAR_K = 5

# ---- prompts（POLARIS_* 标记与 core/llm/fake.py 对齐） ----

GOAL_EXPLORE_SYSTEM = """\
POLARIS_GOAL_EXPLORE — 你是研究目标构建者，通过反复查阅项目文献库，为一个研究种子
构建出扎实、有依据的结构化研究目标（goal）。

探索时必须回答四个问题（checklist）：
①围绕种子已有哪些工作（用工具检索，拿到证据）②真正的研究空白是什么
③我们的独特切入角是什么 ④做这件事需要什么资源。

可用工具：
%(tools)s

每轮只输出一个 JSON 对象（不要输出其他文字或代码块围栏），二选一：
1. 调用工具：{"tool": "工具名", "args": {...}}
2. 探索充分后交付目标：{"finish": {goal}}

goal 格式（所有字段必填）：
{"research_type": "method|benchmark|analysis|survey|application|theory",
 "task": "研究任务（领域内的具体任务）",
 "question": "核心研究问题（一句话）",
 "objectives": ["具体、可检验的研究目标，1-5 条"],
 "scope": {"in_scope": ["范围内"], "out_of_scope": ["范围外"]},
 "success_criteria": ["怎样算成功（可量化优先）"],
 "grounding": [{"paper_id": "库内论文uuid", "why": "该文献与目标的关系（支撑/空白/对比）"}],
 "key_concepts": ["概念名"],
 "resources_needed": {"compute": "算力需求",
                      "data": ["数据集名（标注是否公开可得）"], "time_weeks": 8}}

grounding 必须引用你检索到的真实库内论文（paper_id 取自检索命中），至少 3 篇。
"""

GOAL_REFINE_SYSTEM = """\
POLARIS_GOAL_REFINE — 你是研究目标修订者。人工审批给出了修改意见，请把意见并入
现有研究目标，保持未被意见触及的部分不变。
只输出一个 JSON 对象：{"goal": {与原 goal 相同结构的完整对象}}
"""

RELATED_WORK_SYSTEM = """\
POLARIS_PROPOSAL_RELATED — 你是研究方案的「背景与相关工作」撰写者。
基于研究目标与依据文献，梳理相关工作并明确本工作的定位与差异。

要求：
- goal.grounding 里的每篇库内论文都必须覆盖，引用写作 [[paper:论文uuid]]；
- 给出的外部文献（如有）用 [标题](url) 引用，并说明与本工作的差异；
- 必须包含「本工作与已有工作的差异」小节，逐条对比。

可用工具（需要补充检索时调用）：
%(tools)s

每轮只输出一个 JSON 对象，二选一：
1. {"tool": "工具名", "args": {...}}
2. {"finish": {"content": "markdown 正文",
   "extra_papers": [{"paper_id": "补充引用的库内论文uuid", "why": "补充原因"}]}}
"""

DESIGN_SYSTEM_COMMON = """\
POLARIS_PROPOSAL_DESIGN — 你是研究方案的「研究方案设计」撰写者。
每个设计选择都要给出依据（引用文献 [[paper:uuid]] 或明确论证），直接输出 markdown 正文
（不要输出 JSON、不要代码块围栏）。设计必须服务于研究目标的 objectives 与 success_criteria。
"""

DESIGN_TEMPLATES: dict[str, str] = {
    "method": (
        "本研究是方法型。必须覆盖：方法总体设计、关键创新点（与已有方法的本质差异）、"
        "理论依据或直觉解释、方法的适用边界。"
    ),
    "benchmark": (
        "本研究是评测基准型。必须覆盖：任务定义、数据来源与构建流程、"
        "评测协议（指标/切分/评分方式）、防数据污染措施、基准的区分度设计。"
    ),
    "analysis": (
        "本研究是分析型。必须覆盖：核心假设、分析框架、数据来源、统计/实验方法、混淆变量控制。"
    ),
    "survey": ("本研究是综述型。必须覆盖：综述范围与分类框架、文献筛选标准、对比维度、预期洞见。"),
    "application": (
        "本研究是应用型。必须覆盖：应用场景与需求、系统/流程设计、与既有方案的差异、落地约束。"
    ),
    "theory": (
        "本研究是理论型。必须覆盖：形式化问题定义、理论工具与证明路线、预期定理/界、"
        "与已有理论结果的关系。"
    ),
}

EXPERIMENTS_SYSTEM = """\
POLARIS_PROPOSAL_EXPERIMENTS — 你是研究方案的「实验与评估计划」撰写者。
实验设计必须与 success_criteria 对得上，并落到给定的资源画像内。

只输出一个 JSON 对象：
{"content": "markdown 正文（baselines / datasets / metrics 含主指标 / ablations / 算力粗估）",
 "smoke_plan": {"goal": "最小验证实验要回答什么", "steps": ["1-3 天内可完成的步骤"],
                "metric": "观察什么信号", "expected_signal": "什么结果算方向可行", "est_hours": 8}}
smoke_plan 是「最小验证实验」：1-3 天能出信号、能低成本证伪方向的最小实验。
"""

NOVELTY_SYSTEM = """\
POLARIS_NOVELTY_CHECK — 你是新颖性核查员。对照给出的相似工作清单（库内 + 外部检索），
逐条论证本方案与它们的差异，并给出总判定。

只输出一个 JSON 对象：
{"verdict": "novel" 或 "needs_differentiation" 或 "duplicate",
 "comparisons": [{"title": "相似工作标题", "difference": "本方案与它的本质差异；若无差异要明说"}],
 "reason": "总体判定理由"}

判定标准：与所有相似工作都有清晰差异 → novel；核心思路与某工作接近但可以通过调整设计
拉开差距 → needs_differentiation；与某工作高度重合且没有差异空间 → duplicate。
"""

RISKS_SYSTEM = """\
POLARIS_PROPOSAL_RISKS — 你是研究方案的「风险与备选方案」撰写者。
必须覆盖新颖性核查与资源画像暴露的问题。
只输出一个 JSON 对象：{"risks": [{"risk": "风险", "mitigation": "缓解措施或备选方案"}]}
至少给出 2 条。
"""

TITLE_SYSTEM = """\
POLARIS_PROPOSAL_TITLE — 你是研究方案的定稿者。基于研究目标与各节内容，产出标题、
一句话概述和「预期成果与产出」。
只输出一个 JSON 对象：
{"title": "研究方案标题", "summary": "一句话概述",
 "expected": "预期成果与产出的 markdown 正文（论文/数据集/代码/结论等）"}
"""

REVIEW_SYSTEM = """\
POLARIS_PROPOSAL_REVIEW — 你是专职评审员「%(name)s」，只从一个维度评审这份研究方案：
%(focus)s

只输出一个 JSON 对象：
{"score": 0-10, "must_fix": ["不修复就不该立项的问题（没有就给空数组）"],
 "suggestions": ["改进建议"]}
must_fix 只放真正致命的问题；一般性改进放 suggestions。
"""

REVISE_SYSTEM = """\
POLARIS_PROPOSAL_REVISE — 你是研究方案的作者，评审员给出了必须修复的问题清单。
针对 must_fix 修订对应章节，未涉及的章节不要改动。
只输出一个 JSON 对象：
{"sections": {"related_work|design|experiments|risks": "修订后的完整 markdown"}}
只包含确实修订过的章节。
"""

# 评审员 → Idea.scores 维度映射（docs/api-idea2.md §6）
REVIEWERS: tuple[tuple[str, str, str], ...] = (
    (
        "新颖性评审员",
        "novelty",
        "对照 evidence 中的相似文献，判断方案是否真的新颖、差异论证是否成立",
    ),
    ("方法论评审员", "operability", "设计漏洞、混淆变量、评测有效性、方案是否具体可操作"),
    ("可行性评审员", "feasibility", "算力/数据/时间等资源是否成立，最小验证实验是否真能低成本证伪"),
    ("影响力评审员", "impact", "结论对谁有用、可推广性、成果的潜在影响"),
)

SECTION_KEYS = ("related_work", "design", "experiments", "risks")
SECTION_TITLES = {
    "related_work": "背景与相关工作",
    "design": "研究方案设计",
    "experiments": "实验与评估计划",
    "risks": "风险与备选方案",
}


# ---- 公共小件 ----


def resolve_deep_knobs(raw: Any) -> dict[str, Any]:
    knobs = dict(DEFAULT_DEEP_KNOBS)
    if isinstance(raw, dict):
        for key in DEFAULT_DEEP_KNOBS:
            if raw.get(key) is not None:
                knobs[key] = raw[key]
    return knobs


def _deep_knobs(ctx: ActionContext) -> dict[str, Any]:
    return resolve_deep_knobs(_params(ctx).get("knobs"))


def _seed(ctx: ActionContext) -> dict[str, Any]:
    seed = _params(ctx).get("seed")
    return seed if isinstance(seed, dict) else {"type": "text", "value": ""}


async def _log(ctx: ActionContext, message: str) -> None:
    """voyage 日志流事件（前端探索/检索时间线实时可见）。"""
    if ctx.bus is not None:
        await ctx.bus.publish_voyage_event(ctx.run.id, "log", {"message": message})


def _sections(ctx: ActionContext) -> dict[str, str]:
    sections = ctx.checkpoint.get("proposal_sections")
    return dict(sections) if isinstance(sections, dict) else {}


def _set_section(ctx: ActionContext, key: str, content: str) -> None:
    sections = _sections(ctx)
    sections[key] = content
    ctx.checkpoint["proposal_sections"] = sections


def _goal(ctx: ActionContext) -> dict[str, Any]:
    goal = ctx.checkpoint.get("goal")
    if not isinstance(goal, dict):
        raise ValueError("checkpoint 缺少 goal（目标构建未完成）")
    return goal


def _self_check(passed: bool, reason: str, **extra: Any) -> dict[str, Any]:
    return {"self_check": {"passed": passed, "reason": reason}, **extra}


# ---- goal 结构校验（确定性，docs/api-idea2.md §3） ----


def _require_str_list(value: Any, field: str, *, min_len: int = 1, max_len: int = 20) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"goal.{field} 必须是数组")
    items = [str(v).strip() for v in value if str(v).strip()]
    if not (min_len <= len(items) <= max_len):
        raise ValueError(f"goal.{field} 需要 {min_len}-{max_len} 条非空内容")
    return items


def validate_goal(raw: Any, *, library_ids: set[str], library_size: int) -> dict[str, Any]:
    """机械验收 goal schema；不合法抛 ValueError（诊断给重规划/重试用）。"""
    if not isinstance(raw, dict):
        raise ValueError("goal 必须是 JSON 对象")
    research_type = str(raw.get("research_type") or "").strip()
    if research_type not in RESEARCH_TYPES:
        raise ValueError(f"research_type 必须是 {'/'.join(RESEARCH_TYPES)} 之一")
    task = str(raw.get("task") or "").strip()
    question = str(raw.get("question") or "").strip()
    if not task or not question:
        raise ValueError("task 与 question 不能为空")

    objectives = _require_str_list(raw.get("objectives"), "objectives", min_len=1, max_len=5)
    scope_raw = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
    scope = {
        "in_scope": _require_str_list(scope_raw.get("in_scope"), "scope.in_scope"),
        "out_of_scope": [
            str(v).strip() for v in (scope_raw.get("out_of_scope") or []) if str(v).strip()
        ],
    }
    success_criteria = _require_str_list(raw.get("success_criteria"), "success_criteria")
    key_concepts = _require_str_list(raw.get("key_concepts"), "key_concepts")

    grounding_raw = raw.get("grounding")
    if not isinstance(grounding_raw, list):
        raise ValueError("grounding 必须是数组")
    grounding: list[dict[str, str]] = []
    for entry in grounding_raw:
        if not isinstance(entry, dict):
            continue
        paper_id = str(entry.get("paper_id") or "").strip()
        why = str(entry.get("why") or "").strip()
        if not paper_id or not why:
            raise ValueError("grounding 每条需含 paper_id 与 why")
        if paper_id not in library_ids:
            raise ValueError(f"grounding 引用了库内不存在的论文：{paper_id}")
        if paper_id not in {g["paper_id"] for g in grounding}:
            grounding.append({"paper_id": paper_id, "why": why})
    required = min(3, library_size)
    if len(grounding) < required:
        raise ValueError(f"grounding 至少需要 {required} 篇库内论文（当前 {len(grounding)} 篇）")

    resources_raw = raw.get("resources_needed")
    if not isinstance(resources_raw, dict):
        raise ValueError("resources_needed 必须是对象")
    compute = str(resources_raw.get("compute") or "").strip()
    if not compute:
        raise ValueError("resources_needed.compute 不能为空")
    data = _require_str_list(resources_raw.get("data"), "resources_needed.data")
    try:
        time_weeks = float(resources_raw.get("time_weeks"))
    except (TypeError, ValueError) as e:
        raise ValueError("resources_needed.time_weeks 必须是数字") from e
    if time_weeks <= 0:
        raise ValueError("resources_needed.time_weeks 必须为正")

    return {
        "research_type": research_type,
        "task": task,
        "question": question,
        "objectives": objectives,
        "scope": scope,
        "success_criteria": success_criteria,
        "grounding": grounding,
        "key_concepts": key_concepts,
        "resources_needed": {"compute": compute, "data": data, "time_weeks": time_weeks},
    }


async def _library_index(ctx: ActionContext) -> tuple[set[str], int]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(select(Paper.id).where(Paper.project_id == ctx.run.project_id))
        ).all()
    ids = {str(pid) for (pid,) in rows}
    return ids, len(ids)


# ---- 阶段 1：目标构建 ----


async def _seed_brief(ctx: ActionContext) -> str:
    """种子渲染为探索起点文本；idea 种子继承其 evidence。"""
    seed = _seed(ctx)
    seed_type = str(seed.get("type") or "text")
    value = str(seed.get("value") or "")
    if seed_type == "text":
        return f"种子（自由文本）：{value}"
    async with get_sessionmaker()() as session:
        if seed_type == "paper":
            paper = await session.get(Paper, uuid.UUID(value))
            if paper is not None:
                return (
                    f"种子（库内论文）：{paper.title}\n"
                    f"TL;DR：{paper.tldr or (paper.abstract or '')[:300]}"
                )
        elif seed_type == "concept":
            from app.models.paper import Concept

            concept = await session.get(Concept, uuid.UUID(value))
            if concept is not None:
                return f"种子（概念）：{concept.name}\n定义：{concept.definition or '（无）'}"
        elif seed_type == "idea":
            idea = await session.get(Idea, uuid.UUID(value))
            if idea is not None:
                evidence = json.dumps(idea.evidence or [], ensure_ascii=False)
                return (
                    f"种子（方向草案）：{idea.title}\n概述：{idea.summary or '（无）'}\n"
                    f"草案内容：\n{(idea.content or '')[:1500]}\n草案依据信号：{evidence}"
                )
    return f"种子（{seed_type}，未找到对象，按文本处理）：{value}"


def _trace_summary(trace: list[dict[str, Any]]) -> str:
    if not trace:
        return "（未进行工具探索）"
    parts = [str(t.get("summary") or t.get("tool")) for t in trace]
    return "探索轨迹：" + "；".join(parts[:20])


@register("goal.explore")
async def goal_explore(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("goal"), dict):  # 断点幂等
        return _self_check(True, "goal 已存在（断点续跑）", skipped=True)
    knobs = _deep_knobs(ctx)

    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        statement = _statement(project)
    seed_text = await _seed_brief(ctx)
    library_ids, library_size = await _library_index(ctx)

    opening = f"研究方向：{statement}\n{seed_text}"
    if params.get("diagnosis"):
        opening += f"\n上次构建目标未通过验收，诊断：{params['diagnosis']}\n请针对性修正。"
    system = GOAL_EXPLORE_SYSTEM % {"tools": lit_tools.TOOL_SPECS}

    try:
        finish, trace = await run_tool_loop(
            ctx,
            stage="goal_explore",
            system=system,
            opening=opening,
            tool_names=lit_tools.LIT_TOOL_NAMES,
            max_calls=int(knobs["max_tool_calls"]),
            label="目标构建",
        )
        goal = validate_goal(finish, library_ids=library_ids, library_size=library_size)
    except ValueError as e:
        return _self_check(False, f"目标构建失败：{e}")

    ctx.checkpoint["goal"] = goal
    ctx.checkpoint["goal_trace"] = trace
    # idea_goal 闸门 payload 预置（engine 创建 Gate 时合并，docs/api-idea2.md §4）
    ctx.checkpoint["gate_payload"] = {
        "goal": goal,
        "trace_summary": _trace_summary(trace),
    }
    return _self_check(
        True,
        "goal 已通过结构校验",
        tool_calls=len(trace),
        research_type=goal["research_type"],
        grounding=len(goal["grounding"]),
    )


@register("goal.refine")
async def goal_refine(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """闸门后置动作：审批意见非空时并入 goal（idea_goal / idea_pivot 共用）。"""
    goal = _goal(ctx)
    comment = await _own_gate_comment(ctx)
    pivot_reason = ""
    if params.get("reason") == "duplicate":
        novelty = ctx.checkpoint.get("novelty") or {}
        pivot_reason = (
            "新颖性核查判定与已有工作高度重合，必须调整研究方向。\n"
            f"重合详情：{json.dumps(novelty.get('comparisons') or [], ensure_ascii=False)[:2000]}\n"
        )
    if not comment and not pivot_reason:
        return _self_check(True, "审批未附修改意见，目标保持不变", refined=False)

    library_ids, library_size = await _library_index(ctx)
    user = (
        f"现有研究目标：\n<<<GOAL\n{json.dumps(goal, ensure_ascii=False)}\nGOAL>>>\n"
        f"{pivot_reason}"
        f"审批意见：{comment or '（无，仅按重合详情调整方向）'}"
    )

    def validate(data: Any) -> dict[str, Any]:
        refined = data.get("goal") if isinstance(data, dict) else None
        return validate_goal(refined, library_ids=library_ids, library_size=library_size)

    try:
        refined_goal = await _complete_json(
            ctx, stage="goal_explore", system=GOAL_REFINE_SYSTEM, user=user, validate=validate
        )
    except ValueError as e:
        return _self_check(False, f"目标修订失败：{e}")
    ctx.checkpoint["goal"] = refined_goal
    gate_payload = dict(ctx.checkpoint.get("gate_payload") or {})
    gate_payload["goal"] = refined_goal
    ctx.checkpoint["gate_payload"] = gate_payload
    await _log(ctx, "已按审批意见调整研究目标")
    return _self_check(True, "goal 修订并通过结构校验", refined=True)


async def _own_gate_comment(ctx: ActionContext) -> str:
    """当前步骤自己的闸门 comment（engine 在 checkpoint.gates 记 {step_id: {gate_id}}；
    迁移前的存量 run 按游标键控，做读取回退）。"""
    gates = ctx.checkpoint.get("gates") or {}
    entry = gates.get(str(ctx.step_id)) or gates.get(str(ctx.run.cursor))
    if not isinstance(entry, dict) or not entry.get("gate_id"):
        return ""
    async with get_sessionmaker()() as session:
        gate = await session.get(Gate, uuid.UUID(str(entry["gate_id"])))
    return (gate.comment or "").strip() if gate is not None else ""


# ---- 阶段 2：方案深耕 ----


async def _grounding_papers(ctx: ActionContext) -> list[Paper]:
    goal = _goal(ctx)
    ids = [uuid.UUID(g["paper_id"]) for g in goal.get("grounding") or []]
    if not ids:
        return []
    async with get_sessionmaker()() as session:
        papers = (await session.execute(select(Paper).where(Paper.id.in_(ids)))).scalars().all()
    by_id = {p.id: p for p in papers}
    return [by_id[i] for i in ids if i in by_id]


def _goal_context(goal: dict[str, Any]) -> str:
    return (f"研究目标（goal）：\n{json.dumps(goal, ensure_ascii=False, indent=None)}")[
        :_SECTION_CONTEXT_CHARS
    ]


async def _grounding_context(ctx: ActionContext) -> str:
    goal = _goal(ctx)
    papers = await _grounding_papers(ctx)
    why_by_id = {g["paper_id"]: g["why"] for g in goal.get("grounding") or []}
    parts = []
    for paper in papers:
        excerpt = (paper.wiki_content or paper.abstract or "")[:_GROUNDING_EXCERPT_CHARS]
        parts.append(
            f"[[paper:{paper.id}]] {paper.title}\n"
            f"与目标的关系：{why_by_id.get(str(paper.id), '')}\n{excerpt}"
        )
    return "依据文献：\n" + "\n\n".join(parts) if parts else "依据文献：（无）"


async def _external_search(
    ctx: ActionContext, queries: list[str], *, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """外部检索（S2 优先，OpenAlex 兜底）；失败降级返回 (已得结果, ok=False)。"""
    results: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    ok = True
    s2 = SemanticScholarClient()
    openalex = OpenAlexClient()
    try:
        for query in queries:
            rows: list[dict[str, Any]] = []
            try:
                rows = await s2.search_papers(query, limit=limit)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — S2 失败走 OpenAlex
                try:
                    rows = await openalex.search_works(query, limit=limit)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — 双通道均失败：降级
                    ok = False
            for row in rows:
                title = str(row.get("title") or "").strip()
                key = title.lower()
                if not title or key in seen_titles:
                    continue
                seen_titles.add(key)
                results.append(
                    {
                        "title": title,
                        "year": row.get("year"),
                        "venue": row.get("venue") or row.get("journal"),
                        "url": row.get("url"),
                        "abstract": (str(row.get("abstract") or ""))[:400] or None,
                    }
                )
            await _log(ctx, f"外部检索「{query}」→ 累计 {len(results)} 篇")
    finally:
        await s2.aclose()
        await openalex.aclose()
    return results[: limit * 2], ok


@register("proposal.related_work")
async def proposal_related_work(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if _sections(ctx).get("related_work"):  # 断点幂等
        return _self_check(True, "related_work 已完成（断点续跑）", skipped=True)
    knobs = _deep_knobs(ctx)
    goal = _goal(ctx)

    external: list[dict[str, Any]] = []
    if knobs["external_search"]:
        external, _ok = await _external_search(
            ctx, [goal["task"], goal["question"]], limit=_EXTERNAL_SIMILAR_K
        )
        if external:
            existing = list(ctx.checkpoint.get("evidence_external") or [])
            titles = {e.get("title") for e in existing}
            existing.extend(
                {**e, "why": "相关工作外部检索", "source": "external"}
                for e in external
                if e["title"] not in titles
            )
            ctx.checkpoint["evidence_external"] = existing

    grounding_ids = [g["paper_id"] for g in goal.get("grounding") or []]
    opening = (
        f"{_goal_context(goal)}\n\n{await _grounding_context(ctx)}\n\n"
        f"必须覆盖的库内论文 id：{json.dumps(grounding_ids, ensure_ascii=False)}\n"
        f"外部相关文献（须论证差异）：{json.dumps(external, ensure_ascii=False)[:4000]}"
    )
    if params.get("diagnosis"):
        opening += f"\n上次未通过验收，诊断：{params['diagnosis']}"

    try:
        finish, trace = await run_tool_loop(
            ctx,
            stage="proposal",
            system=RELATED_WORK_SYSTEM % {"tools": lit_tools.TOOL_SPECS},
            opening=opening,
            tool_names=lit_tools.LIT_TOOL_NAMES,
            max_calls=6,
            label="相关工作",
        )
    except ValueError as e:
        return _self_check(False, str(e))
    content = str(finish.get("content") or "").strip()
    if not content:
        return _self_check(False, "related_work 正文为空")
    missing = [pid for pid in grounding_ids if f"[[paper:{pid}]]" not in content]
    if missing:
        return _self_check(False, f"related_work 未覆盖 grounding 论文：{', '.join(missing[:5])}")

    # 补充引用的库内论文 → evidence
    extra = [
        e for e in (finish.get("extra_papers") or []) if isinstance(e, dict) and e.get("paper_id")
    ]
    if extra:
        ctx.checkpoint["evidence_library_extra"] = extra
    _set_section(ctx, "related_work", content)
    return _self_check(True, "related_work 覆盖全部 grounding 论文", tool_calls=len(trace))


@register("proposal.design")
async def proposal_design(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    goal = _goal(ctx)
    if _sections(ctx).get("design") and not params.get("diagnosis"):  # 断点幂等（回炉除外）
        return _self_check(True, "design 已完成（断点续跑）", skipped=True)
    template = DESIGN_TEMPLATES.get(goal["research_type"], DESIGN_TEMPLATES["method"])
    user = (
        f"{_goal_context(goal)}\n\n{await _grounding_context(ctx)}\n\n"
        f"已完成的「背景与相关工作」：\n{_sections(ctx).get('related_work', '')[:3000]}"
    )
    if params.get("diagnosis"):
        user += f"\n上一版设计未通过（须针对性修改）：{params['diagnosis']}"
    result = await ctx.llm.complete(
        "proposal",
        [
            Message(role="system", content=f"{DESIGN_SYSTEM_COMMON}\n{template}"),
            Message(role="user", content=user),
        ],
        user_id=ctx.run.created_by,
        project_id=ctx.run.project_id,
        voyage_id=ctx.run.id,
    )
    content = result.content.strip()
    if len(content) < 200:
        return _self_check(False, f"design 正文过短（{len(content)} 字符），设计不成立")
    _set_section(ctx, "design", content)
    return _self_check(True, "design 已产出", research_type=goal["research_type"])


async def _resources_profile(ctx: ActionContext) -> str:
    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
    definition = project.definition if isinstance(project.definition, dict) else {}
    resources = definition.get("resources")
    if resources:
        return f"项目资源画像：{json.dumps(resources, ensure_ascii=False)[:1500]}"
    return "项目资源画像：（未配置，按通用实验室资源假设，并在正文显式标注该假设）"


@register("proposal.experiments")
async def proposal_experiments(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if _sections(ctx).get("experiments") and not params.get("diagnosis"):  # 断点幂等
        return _self_check(True, "experiments 已完成（断点续跑）", skipped=True)
    goal = _goal(ctx)
    user = (
        f"{_goal_context(goal)}\n\n{await _resources_profile(ctx)}\n\n"
        f"研究方案设计：\n{_sections(ctx).get('design', '')[:4000]}"
    )
    if params.get("diagnosis"):
        user += f"\n上次未通过验收，诊断：{params['diagnosis']}"

    def validate(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("payload 不是对象")
        content = str(data.get("content") or "").strip()
        smoke = data.get("smoke_plan")
        if not content:
            raise ValueError("content 为空")
        if not isinstance(smoke, dict):
            raise ValueError("缺少 smoke_plan（最小验证实验）")
        steps = [str(s).strip() for s in (smoke.get("steps") or []) if str(s).strip()]
        metric = str(smoke.get("metric") or "").strip()
        if not steps or not metric:
            raise ValueError("smoke_plan 需含非空 steps 与 metric")
        return {
            "content": content,
            "smoke_plan": {
                "goal": str(smoke.get("goal") or "").strip(),
                "steps": steps,
                "metric": metric,
                "expected_signal": str(smoke.get("expected_signal") or "").strip(),
                "est_hours": smoke.get("est_hours"),
            },
        }

    try:
        payload = await _complete_json(
            ctx, stage="proposal", system=EXPERIMENTS_SYSTEM, user=user, validate=validate
        )
    except ValueError as e:
        return _self_check(False, f"experiments 产出不合法：{e}")
    smoke = payload["smoke_plan"]
    smoke_md = (
        "\n\n### 最小验证实验\n\n"
        f"- 目的：{smoke['goal'] or '（低成本证伪方向）'}\n"
        + "".join(f"- 步骤：{s}\n" for s in smoke["steps"])
        + f"- 观察信号：{smoke['metric']}\n"
        f"- 可行判据：{smoke['expected_signal'] or '（见正文）'}\n"
    )
    _set_section(ctx, "experiments", payload["content"] + smoke_md)
    ctx.checkpoint["smoke_plan"] = smoke
    return _self_check(True, "experiments 含最小验证实验", smoke_steps=len(smoke["steps"]))


async def _internal_similar(ctx: ActionContext, query_text: str) -> list[dict[str, Any]]:
    """库内相似论文：embedding 余弦 top-k；embedding 不可用降级关键词检索。"""
    async with get_sessionmaker()() as session:
        papers = (
            (
                await session.execute(
                    select(Paper).where(
                        Paper.project_id == ctx.run.project_id, Paper.embedding.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        if papers:
            try:
                vectors = await ctx.llm.embed(
                    [query_text[:2000]],
                    user_id=ctx.run.created_by,
                    project_id=ctx.run.project_id,
                    voyage_id=ctx.run.id,
                )
                scored = [(cosine_similarity(vectors[0], list(p.embedding)), p) for p in papers]
                scored.sort(key=lambda x: -x[0])
                return [
                    {
                        "paper_id": str(p.id),
                        "title": p.title,
                        "similarity": round(score, 3),
                        "tldr": p.tldr or (p.abstract or "")[:200],
                    }
                    for score, p in scored[:_INTERNAL_SIMILAR_K]
                ]
            except NotImplementedError:
                pass
        from app.services.papers import keyword_search_papers

        goal = _goal(ctx)
        rows = await keyword_search_papers(
            session, project_id=ctx.run.project_id, q=goal["task"][:60], limit=_INTERNAL_SIMILAR_K
        )
        return [
            {
                "paper_id": str(p.id),
                "title": p.title,
                "similarity": score,
                "tldr": p.tldr or (p.abstract or "")[:200],
            }
            for p, score in rows
        ]


@register("proposal.novelty_check")
async def proposal_novelty_check(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    existing = ctx.checkpoint.get("novelty")
    if isinstance(existing, dict) and existing.get("verdict") == "novel":  # 断点幂等
        return _self_check(True, "novelty 已判定 novel（断点续跑）", skipped=True)
    knobs = _deep_knobs(ctx)
    goal = _goal(ctx)
    design = _sections(ctx).get("design", "")

    query_text = f"{goal['question']}\n{design[:500]}"
    internal = await _internal_similar(ctx, query_text)
    external: list[dict[str, Any]] = []
    external_ok = True
    if knobs["external_search"]:
        external, external_ok = await _external_search(
            ctx, [goal["task"], goal["question"]], limit=_EXTERNAL_SIMILAR_K
        )

    similar_block = json.dumps({"library": internal, "external": external}, ensure_ascii=False)
    user = (
        f"{_goal_context(goal)}\n\n本方案设计概要：\n{design[:3000]}\n\n"
        f"相似工作清单：\n{similar_block[:6000]}"
    )

    def validate(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict) or data.get("verdict") not in (
            "novel",
            "needs_differentiation",
            "duplicate",
        ):
            raise ValueError('expected {"verdict": "novel|needs_differentiation|duplicate"}')
        comparisons = [
            {"title": str(c.get("title") or ""), "difference": str(c.get("difference") or "")}
            for c in (data.get("comparisons") or [])
            if isinstance(c, dict) and c.get("title")
        ]
        return {
            "verdict": str(data["verdict"]),
            "comparisons": comparisons,
            "reason": str(data.get("reason") or ""),
        }

    try:
        judged = await _complete_json(
            ctx, stage="proposal", system=NOVELTY_SYSTEM, user=user, validate=validate
        )
    except ValueError as e:
        return _self_check(False, f"novelty 判定不合法：{e}")

    novelty = {
        **judged,
        "similar_library": internal,
        "similar_external": external,
        "external_done": bool(knobs["external_search"]) and external_ok,
    }
    ctx.checkpoint["novelty"] = novelty
    verdict = judged["verdict"]
    if verdict == "novel":
        _set_section(ctx, "novelty", _render_novelty(novelty))
        return _self_check(True, "新颖性核查通过（novel）", comparisons=len(judged["comparisons"]))
    if verdict == "needs_differentiation":
        return _self_check(
            False,
            "NEEDS_DIFFERENTIATION: " + (judged["reason"] or "与相似工作差异不足，需调整设计"),
        )
    # duplicate → idea_pivot 闸门 payload 预置，重规划插入带闸门的 goal.refine
    ctx.checkpoint["gate_payload"] = {
        "goal": goal,
        "reason": judged["reason"],
        "comparisons": judged["comparisons"],
        "similar_titles": [s.get("title") for s in internal + external][:10],
    }
    return _self_check(False, "DUPLICATE: " + (judged["reason"] or "与已有工作高度重合"))


def _render_novelty(novelty: dict[str, Any]) -> str:
    lines = ["以下为相似工作逐条差异论证：", ""]
    for comp in novelty.get("comparisons") or []:
        lines.append(f"- **{comp['title']}**：{comp['difference']}")
    if not (novelty.get("comparisons") or []):
        lines.append("- 未检索到高度相似的工作。")
    if not novelty.get("external_done"):
        lines.append("")
        lines.append("> 注意：外部检索未完成（仅核查了库内文献），结论以库内为准。")
    if novelty.get("reason"):
        lines.append("")
        lines.append(f"**总体判定**：novel——{novelty['reason']}")
    return "\n".join(lines)


@register("proposal.risks")
async def proposal_risks(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if _sections(ctx).get("risks") and not params.get("diagnosis"):  # 断点幂等
        return _self_check(True, "risks 已完成（断点续跑）", skipped=True)
    goal = _goal(ctx)
    novelty = ctx.checkpoint.get("novelty") or {}
    user = (
        f"{_goal_context(goal)}\n\n{await _resources_profile(ctx)}\n\n"
        "新颖性核查结论："
        f"{json.dumps(novelty.get('comparisons') or [], ensure_ascii=False)[:2000]}\n"
        f"实验计划概要：\n{_sections(ctx).get('experiments', '')[:2000]}"
    )

    def validate(data: Any) -> list[dict[str, str]]:
        risks = data.get("risks") if isinstance(data, dict) else None
        if not isinstance(risks, list):
            raise ValueError('expected {"risks": [...]}')
        normalized = [
            {
                "risk": str(r.get("risk") or "").strip(),
                "mitigation": str(r.get("mitigation") or "").strip(),
            }
            for r in risks
            if isinstance(r, dict) and str(r.get("risk") or "").strip()
        ]
        if len(normalized) < 2 or any(not r["mitigation"] for r in normalized):
            raise ValueError("至少 2 条风险且每条都要有缓解/备选方案")
        return normalized

    try:
        risks = await _complete_json(
            ctx, stage="proposal", system=RISKS_SYSTEM, user=user, validate=validate
        )
    except ValueError as e:
        return _self_check(False, f"risks 产出不合法：{e}")
    content = "\n".join(f"- **风险**：{r['risk']}\n  - 缓解/备选：{r['mitigation']}" for r in risks)
    _set_section(ctx, "risks", content)
    return _self_check(True, f"risks 共 {len(risks)} 条", risks=len(risks))


# ---- 汇编与入库 ----


def _render_goal_md(goal: dict[str, Any]) -> str:
    resources = goal.get("resources_needed") or {}
    lines = [
        f"- **研究类型**：{goal['research_type']}",
        f"- **研究任务**：{goal['task']}",
        f"- **核心问题**：{goal['question']}",
        "- **研究目标**：",
        *(f"  {i}. {o}" for i, o in enumerate(goal["objectives"], start=1)),
        "- **成功标准**：",
        *(f"  - {c}" for c in goal["success_criteria"]),
        f"- **范围内**：{'；'.join(goal['scope']['in_scope'])}",
    ]
    if goal["scope"].get("out_of_scope"):
        lines.append(f"- **范围外**：{'；'.join(goal['scope']['out_of_scope'])}")
    lines.append(
        f"- **资源需求**：算力——{resources.get('compute')}；"
        f"数据——{'；'.join(resources.get('data') or [])}；"
        f"预计 {resources.get('time_weeks')} 周"
    )
    return "\n".join(lines)


def _compose_content(
    title: str,
    goal: dict[str, Any],
    sections: dict[str, str],
    expected: str,
    leftovers: list[str],
) -> str:
    parts = [
        f"# {title}",
        "## 研究目标\n\n" + _render_goal_md(goal),
        "## 背景与相关工作\n\n" + sections.get("related_work", "（缺）"),
        "## 研究方案设计\n\n" + sections.get("design", "（缺）"),
        "## 实验与评估计划\n\n" + sections.get("experiments", "（缺）"),
        "## 预期成果与产出\n\n" + (expected or "（缺）"),
        "## 风险与备选方案\n\n" + sections.get("risks", "（缺）"),
        "## 新颖性核查\n\n" + sections.get("novelty", "（缺）"),
    ]
    if leftovers:
        parts.append("## 遗留问题\n\n" + "\n".join(f"- {item}" for item in leftovers))
    else:
        parts.append("## 遗留问题\n\n（无——评审提出的必须修复项已全部处理）")
    return "\n\n".join(parts)


async def _build_evidence(ctx: ActionContext) -> list[dict[str, Any]]:
    goal = _goal(ctx)
    papers = await _grounding_papers(ctx)
    titles = {str(p.id): p.title for p in papers}
    why_by_id = {g["paper_id"]: g["why"] for g in goal.get("grounding") or []}
    evidence: list[dict[str, Any]] = [
        {
            "paper_id": pid,
            "title": titles.get(pid, ""),
            "url": None,
            "why": why,
            "source": "library",
        }
        for pid, why in why_by_id.items()
    ]
    for extra in ctx.checkpoint.get("evidence_library_extra") or []:
        pid = str(extra.get("paper_id"))
        if pid not in why_by_id:
            evidence.append(
                {
                    "paper_id": pid,
                    "title": str(extra.get("title") or ""),
                    "url": None,
                    "why": str(extra.get("why") or "相关工作补充引用"),
                    "source": "library",
                }
            )
    for ext in ctx.checkpoint.get("evidence_external") or []:
        evidence.append(
            {
                "paper_id": None,
                "title": str(ext.get("title") or ""),
                "url": ext.get("url"),
                "why": str(ext.get("why") or "外部相关文献"),
                "source": "external",
            }
        )
    # 种子草案的信号依据一并继承
    seed = _seed(ctx)
    if seed.get("type") == "idea":
        async with get_sessionmaker()() as session:
            try:
                sketch = await session.get(Idea, uuid.UUID(str(seed.get("value"))))
            except ValueError:
                sketch = None
        for item in (sketch.evidence if sketch is not None else None) or []:
            if isinstance(item, dict) and item.get("source") == "signal":
                evidence.append(item)
    return evidence


@register("proposal.assemble")
async def proposal_assemble(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if ctx.checkpoint.get("idea_id"):  # 断点幂等
        return _self_check(
            True, "idea 已入库（断点续跑）", skipped=True, idea_id=ctx.checkpoint["idea_id"]
        )
    goal = _goal(ctx)
    sections = _sections(ctx)
    missing = [k for k in (*SECTION_KEYS, "novelty") if not sections.get(k)]
    if missing:
        return _self_check(False, f"章节缺失，无法汇编：{', '.join(missing)}")

    def validate_title(data: Any) -> dict[str, str]:
        if not isinstance(data, dict) or not str(data.get("title") or "").strip():
            raise ValueError('expected {"title": ...}')
        return {
            "title": str(data["title"]).strip()[:512],
            "summary": str(data.get("summary") or "").strip(),
            "expected": str(data.get("expected") or "").strip(),
        }

    meta = await _complete_json(
        ctx,
        stage="proposal",
        system=TITLE_SYSTEM,
        user=(
            f"{_goal_context(goal)}\n\n各节概要：\n"
            + "\n".join(f"### {SECTION_TITLES.get(k, k)}\n{v[:800]}" for k, v in sections.items())
        ),
        validate=validate_title,
    )
    content = _compose_content(meta["title"], goal, sections, meta["expected"], [])
    ctx.checkpoint["proposal_expected"] = meta["expected"]

    # 四维自评（临时分，评审-修订后被终评覆盖）
    scores, rationale = None, None
    try:
        scores, rationale = await _complete_json(
            ctx,
            stage="proposal",
            system=SCORE_SYSTEM_PROMPT,
            user=f"想法标题:{meta['title']}\n一句话概述:{meta['summary']}\n详情:\n{content[:6000]}",
            validate=_validate_scores,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — 自评失败不阻塞入库（终评会补）
        pass

    evidence = await _build_evidence(ctx)
    seed = _seed(ctx)
    seed_idea_id: uuid.UUID | None = None
    if seed.get("type") == "idea":
        try:
            seed_idea_id = uuid.UUID(str(seed.get("value")))
        except ValueError:
            seed_idea_id = None

    embedding = None
    try:
        vectors = await ctx.llm.embed(
            [f"{meta['title']}\n{meta['summary']}"[:2000]],
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
        )
        embedding = vectors[0]
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — 嵌入失败不阻塞入库
        pass

    async with get_sessionmaker()() as session:
        idea = Idea(
            project_id=ctx.run.project_id,
            title=meta["title"],
            summary=meta["summary"] or None,
            content=content,
            scores=scores,
            score_rationale=rationale,
            status="candidate",
            depth="proposal",
            research_type=goal["research_type"],
            goal={**goal, "smoke_plan": ctx.checkpoint.get("smoke_plan")},
            evidence=evidence,
            seed_idea_id=seed_idea_id,
            parent_paper_ids=[g["paper_id"] for g in goal.get("grounding") or []],
            embedding=embedding,
        )
        session.add(idea)
        await session.flush()
        idea_id = str(idea.id)
        session.add(
            Activity(
                project_id=ctx.run.project_id,
                actor="agent:idea-proposal",
                kind="idea.proposal_created",
                message=f"研究方案「{idea.title}」已生成，进入评审修订",
                payload={"voyage_id": str(ctx.run.id), "idea_id": idea_id},
            )
        )
        await session.commit()

    ctx.checkpoint["idea_id"] = idea_id
    await ctx.notify(
        {"type": "idea.created", "project_id": str(ctx.run.project_id), "idea_id": idea_id}
    )
    return _self_check(True, "Research Proposal 已汇编入库", idea_id=idea_id)


# ---- 阶段 3：评审-修订循环 ----


def _review_message_text(review: dict[str, Any]) -> str:
    lines = [f"评分：{review['score']}/10"]
    if review["must_fix"]:
        lines.append("必须修复：")
        lines.extend(f"- {m}" for m in review["must_fix"])
    else:
        lines.append("必须修复：（无）")
    if review["suggestions"]:
        lines.append("建议：")
        lines.extend(f"- {s}" for s in review["suggestions"])
    return "\n".join(lines)


def _validate_review(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("score"), int | float):
        raise ValueError('expected {"score": number, ...}')
    return {
        "score": min(10.0, max(0.0, float(data["score"]))),
        "must_fix": [str(m).strip() for m in (data.get("must_fix") or []) if str(m).strip()],
        "suggestions": [str(s).strip() for s in (data.get("suggestions") or []) if str(s).strip()],
    }


@register("proposal.review_revise")
async def proposal_review_revise(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if ctx.checkpoint.get("review_done"):  # 断点幂等
        return _self_check(True, "评审-修订已完成（断点续跑）", skipped=True)
    idea_id_raw = ctx.checkpoint.get("idea_id")
    if not idea_id_raw:
        return _self_check(False, "idea 尚未入库，无法评审")
    idea_id = uuid.UUID(str(idea_id_raw))
    knobs = _deep_knobs(ctx)
    revise_rounds = int(knobs["revise_rounds"])
    goal = _goal(ctx)
    novelty = ctx.checkpoint.get("novelty") or {}

    async with get_sessionmaker()() as session:
        review_session = ReviewSession(
            target_type="idea_revision",
            target_id=idea_id,
            payload={"voyage_id": str(ctx.run.id), "rounds": []},
        )
        session.add(review_session)
        await session.commit()
        await session.refresh(review_session)
        session_id = review_session.id

    sections = _sections(ctx)
    expected = str(ctx.checkpoint.get("proposal_expected") or "")
    rounds_meta: list[dict[str, Any]] = []
    last_reviews: dict[str, dict[str, Any]] = {}
    revisions_done = 0
    title = ""

    async with get_sessionmaker()() as session:
        idea = await session.get(Idea, idea_id)
        if idea is None:
            return _self_check(False, "idea 不存在")
        title = idea.title

    round_no = 0
    while True:
        round_no += 1
        content = _compose_content(title, goal, sections, expected, [])
        reviews: dict[str, dict[str, Any]] = {}
        for name, dim, focus in REVIEWERS:
            extra_context = ""
            if dim == "novelty":
                extra_context = (
                    "\n相似工作与差异论证："
                    + json.dumps(novelty.get("comparisons") or [], ensure_ascii=False)[:2000]
                )
            elif dim == "feasibility":
                extra_context = "\n" + await _resources_profile(ctx)
            try:
                reviews[dim] = await _complete_json(
                    ctx,
                    stage="proposal_review",
                    system=REVIEW_SYSTEM % {"name": name, "focus": focus},
                    user=f"研究方案全文：\n{content[:_PROPOSAL_REVIEW_CHARS]}{extra_context}",
                    validate=_validate_review,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 单评审员失败：中性分不设 must_fix
                reviews[dim] = {
                    "score": 5.0,
                    "must_fix": [],
                    "suggestions": [f"评审调用失败（{type(e).__name__}），本维度未获有效意见"],
                }
            await _persist_review_message(
                ctx,
                session_id,
                author_name=name,
                content=_review_message_text(reviews[dim]),
                round_no=round_no,
            )
        last_reviews = reviews
        must_fix_all = [m for r in reviews.values() for m in r["must_fix"]]
        rounds_meta.append(
            {
                "round": round_no,
                "scores": {dim: r["score"] for dim, r in reviews.items()},
                "must_fix_count": len(must_fix_all),
            }
        )
        await _log(ctx, f"评审第 {round_no} 轮：{len(must_fix_all)} 条必须修复")
        if not must_fix_all or revisions_done >= revise_rounds:
            break

        # 作者修订（只改被点名的章节）
        try:
            patch = await _complete_json(
                ctx,
                stage="proposal",
                system=REVISE_SYSTEM,
                user=(
                    f"must_fix 清单：{json.dumps(must_fix_all, ensure_ascii=False)}\n\n"
                    f"当前各章节：\n"
                    + "\n\n".join(
                        f"<<{key}>>\n{sections.get(key, '')[:3000]}" for key in SECTION_KEYS
                    )
                ),
                validate=_validate_revision,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 修订失败：终止循环，剩余 must_fix 记遗留
            await _log(ctx, f"修订调用失败，保留遗留问题：{type(e).__name__}")
            break
        for key, new_content in patch.items():
            sections[key] = new_content
        ctx.checkpoint["proposal_sections"] = dict(sections)
        revisions_done += 1
        await _persist_review_message(
            ctx,
            session_id,
            author_name="作者（AI）",
            content="已按必须修复清单修订章节：" + "、".join(SECTION_TITLES[k] for k in patch),
            round_no=round_no,
        )

    # 终评落库：分数/理由/遗留问题/最终正文
    leftovers = [m for r in last_reviews.values() for m in r["must_fix"]]
    final_scores = {dim: r["score"] for dim, r in last_reviews.items()}
    final_rationale = {
        dim: "；".join((r["must_fix"] + r["suggestions"])[:3]) or "（无补充意见）"
        for dim, r in last_reviews.items()
    }
    final_content = _compose_content(title, goal, sections, expected, leftovers)
    async with get_sessionmaker()() as session:
        idea = await session.get(Idea, idea_id)
        if idea is None:
            return _self_check(False, "idea 不存在（评审后落库失败）")
        idea.scores = final_scores
        idea.score_rationale = final_rationale
        idea.content = final_content
        review_session = await session.get(ReviewSession, session_id)
        if review_session is not None:
            review_session.status = "closed"
            review_session.payload = dict(review_session.payload or {}) | {
                "rounds": rounds_meta,
                "leftover_must_fix": leftovers,
            }
        session.add(
            Activity(
                project_id=ctx.run.project_id,
                actor="agent:idea-proposal",
                kind="idea.proposal_reviewed",
                message=(
                    f"研究方案「{idea.title}」评审修订完成"
                    f"（{round_no} 轮评审，遗留 {len(leftovers)} 条）"
                ),
                payload={"idea_id": str(idea.id), "rounds": rounds_meta},
            )
        )
        await session.commit()

    ctx.checkpoint["review_done"] = True
    await ctx.notify({"type": "idea.status", "idea_id": str(idea_id), "status": "candidate"})
    return _self_check(
        True,
        f"评审-修订完成（{round_no} 轮，遗留 {len(leftovers)} 条）",
        rounds=round_no,
        revisions=revisions_done,
        leftovers=len(leftovers),
        scores=final_scores,
    )


def _validate_revision(data: Any) -> dict[str, str]:
    sections = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(sections, dict):
        raise ValueError('expected {"sections": {...}}')
    patch = {
        key: str(value).strip()
        for key, value in sections.items()
        if key in SECTION_KEYS and str(value).strip()
    }
    return patch


async def _persist_review_message(
    ctx: ActionContext,
    session_id: uuid.UUID,
    *,
    author_name: str,
    content: str,
    round_no: int,
) -> None:
    async with get_sessionmaker()() as session:
        message = ReviewMessage(
            session_id=session_id,
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
            "session_id": str(session_id),
            "project_id": str(ctx.run.project_id),
            "message": serialize_message(message),
        }
    )
