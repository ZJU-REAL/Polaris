"""discovery 任务的树搜索动作（#642，设计报告 §8.2，P2 D2）。

四个动作围绕假设树（services/hypothesis_tree.py，#640）展开：

- ``hypothesis.seed``：树空时按研究方向生成根假设入树；
- ``hypothesis.expand``：对当前最优 open 节点跑四段假设管线（#648，
  services/hypothesis_pipeline.py）：generate 产最多 4 个子候选，逐个
  ground → novelty → feasibility → score 写进节点，低分自动剪枝；
- ``hypothesis.prune``：显式剪枝入口，写 score 并级联剪枝（留痕不删）；
- ``discovery.summarize``：把整棵树（含被剪分支）汇总成研究方案产物后收束；
- ``discovery.write_ideas``：把留下来的假设写成课题里的想法（证据与立场原样带上），
  汇总之后跑。没有它，探索的结果只是任务里的两个 JSON 产物，进不了想法列表，
  也就到不了实验。

params.tournament=True 时是深度模式（#653）：每轮扩展落库后跑一场锦标赛
（services/hypothesis_tournament.py）——score 前 6 的 open|expanded 假设两两
对比、win-rate 与 pipeline score 对半混成新 score 写回节点，比较记录进
checkpoint["discovery"]["tournament"]（供 D6 披露、API /tournament 消费）。

**计划不是线性清单**：初始计划只有播种一步，之后每一轮由动作按树状态给出
plan_signal（expand / summarize），经 plan_edit.discovery_signal_edits 确定性
分支表追加下一个节点——树才是真源，决策与所选节点 id 记入
checkpoint["discovery"]，恢复语义见各动作的重放注释。

**每节点记账**：seed/expand 的 LLM token 用量按节点 id 记进
checkpoint["discovery"]["node_usage"]（只记不限；不动 hypothesis_nodes 表结构，
避免与并行迁移抢 alembic 链——树可视化要用时 D5 再决定是否上迁移）。
"""

import asyncio
import json
import re
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select

from app.agents.voyage.actions import ActionContext, register
from app.core.db import get_sessionmaker
from app.core.llm import budgets
from app.core.llm.base import Message
from app.core.llm.errors import LLMRefusalError, LLMSchemaError, LLMTruncatedError
from app.models.activity import Activity
from app.models.hypothesis import HypothesisNode
from app.models.idea import Idea
from app.models.paper import Paper
from app.models.voyage import VoyageRun
from app.services import discovery_disclosure, idea_dedup
from app.services import hypothesis_pipeline as pipeline
from app.services import hypothesis_tournament as tournament_service
from app.services import hypothesis_tree as tree_service
from app.services import voyages as voyages_service
from app.services.embedding import embed_documents, upsert_idea_vector

# LLM 环节：结构化 JSON 生成，走中档耐心（core/llm/router.py 的 _MEDIUM_CALL_STAGES）
_STAGE = "discovery_plan"

# 每轮扩展的子候选上限：管线 generate 按它要候选，多给截断（去重后可能更少）
_MAX_CHILDREN = budgets.DISCOVERY_CHILDREN_PER_ROUND

SEED_SYSTEM_PROMPT = """\
POLARIS_DISCOVERY_SEED
你是 Navigator，负责为一个研究方向提出树搜索的根假设：一条值得展开验证的、
可检验的核心研究假设。输出一个 JSON 对象：
{"statement": "假设陈述（一句话，可检验）", "rationale": "一句话依据", \
"score": 0 到 1 的先验看好程度}
"""


class SeedOut(BaseModel):
    statement: pipeline.NonEmptyStr
    rationale: str
    score: float


SUMMARY_SYSTEM_PROMPT = """\
POLARIS_DISCOVERY_SUMMARY
你是 Navigator，负责总结一次假设树探索：概述探索了哪些假设、哪些分支被放弃
及原因、目前最有希望的方向。直接输出总结文本（markdown，不要代码块围栏）。
"""


# ---- checkpoint["discovery"] 工作区 ----


def _state(ctx: ActionContext) -> dict[str, Any]:
    """checkpoint 里的 discovery 工作区（决策留痕 + 轮次账本 + 每节点用量）。

    engine 在动作结束后整体回写 run.checkpoint，这里就地改嵌套 dict 即可。
    """
    state = ctx.checkpoint.get("discovery")
    if not isinstance(state, dict):
        state = {}
        ctx.checkpoint["discovery"] = state
    state.setdefault("rounds", {})
    state.setdefault("decisions", [])
    state.setdefault("node_usage", {})
    return state


def _params_of(ctx: ActionContext) -> dict[str, Any]:
    params = ctx.checkpoint.get("params") if isinstance(ctx.checkpoint, dict) else None
    return params if isinstance(params, dict) else {}


def _max_expansions(ctx: ActionContext) -> int:
    try:
        return max(
            0, int(_params_of(ctx).get("max_expansions", budgets.DISCOVERY_DEFAULT_EXPANSIONS))
        )
    except (TypeError, ValueError):
        return budgets.DISCOVERY_DEFAULT_EXPANSIONS


def _tournament_enabled(ctx: ActionContext) -> bool:
    return bool(_params_of(ctx).get("tournament"))


def _emit_ideas(ctx: ActionContext) -> bool:
    """汇总之后要不要把假设写成想法。默认写——只有创建时显式传了 false 才不写，
    所以这个参数出现之前建的任务恢复后也会写。"""
    return _params_of(ctx).get("emit_ideas") is not False


def _direction(ctx: ActionContext) -> str:
    return str(_params_of(ctx).get("direction") or ctx.run.goal or "").strip()


def _require_library_id(ctx: ActionContext) -> uuid.UUID:
    """discovery 的库关联（#648 起 params.library_id 必填，创建入口已校验可见性）。

    优先 run.library_id（创建时落列，费用记账与可见性都按它走）；直建的存量/
    测试 run 兜底读 params。两处都没有直接报错——四段管线的检索边界就是这个库，
    没有库关联的 discovery 无从接地。
    """
    if ctx.run.library_id is not None:
        return ctx.run.library_id
    raw = _params_of(ctx).get("library_id")
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        raise ValueError("discovery 任务缺少文献库关联（params.library_id）") from None


def _record_decision(
    ctx: ActionContext,
    *,
    action: str,
    decision: str,
    node_id: str | None,
    why: str,
    round_no: int | None = None,
    prune: dict[str, Any] | None = None,
) -> None:
    """每轮决策留痕（哪个节点、为什么）：SSE 不持久，恢复与审计都要靠它。

    ``prune``：自动剪枝的结构化原因（:func:`pipeline.prune_reason`），与 ``why`` 的
    中文句子并存——以前的记录只有 ``why``。
    """
    entry: dict[str, Any] = {
        "action": action,
        "round": round_no,
        "decision": decision,
        "node_id": node_id,
        "why": why,
    }
    if prune is not None:
        entry["prune"] = prune
    _state(ctx)["decisions"].append(entry)


def _account_node_usage(ctx: ActionContext, node_id: str, usage: dict[str, Any]) -> None:
    """把一次 LLM 调用的 token 记到节点头上（只记不限）。"""
    ledger = _state(ctx)["node_usage"]
    entry = dict(ledger.get(node_id) or {"prompt_tokens": 0, "completion_tokens": 0})
    entry["prompt_tokens"] = int(entry.get("prompt_tokens", 0)) + int(
        usage.get("prompt_tokens", 0) or 0
    )
    entry["completion_tokens"] = int(entry.get("completion_tokens", 0)) + int(
        usage.get("completion_tokens", 0) or 0
    )
    ledger[node_id] = entry


def _clamp_score(raw: Any) -> float | None:
    try:
        return min(1.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return None


# ---- 树状态 → 下一步信号（决策的唯一出口，seed/expand 共用） ----


async def _expanded_count(session, run_id) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(HypothesisNode)
                .where(HypothesisNode.run_id == run_id, HypothesisNode.status == "expanded")
            )
        ).scalar_one()
    )


async def _next_signal(session, ctx: ActionContext) -> tuple[dict[str, Any], str]:
    """按「checkpoint 轮次账本 + 树状态」决定下一步，返回 (plan_signal, why)。

    扩展数取 max(账本轮数, 树上 expanded 节点数)：正常路径两者一致；worker 在
    「子节点已落库、checkpoint 尚未回写」的窗口被杀时账本会少一轮，树上的
    expanded 计数把它补回来，保证重启后不多扩一轮（见 hypothesis_expand 的
    重放注释）。
    """
    state = _state(ctx)
    done = max(len(state["rounds"]), await _expanded_count(session, ctx.run.id))
    limit = _max_expansions(ctx)
    best = await tree_service.best_open_node(session, ctx.run.id)
    if best is None:
        why = "树上已无 open 节点，转入汇总"
        return {"decision": "summarize", "reason": why, "emit_ideas": _emit_ideas(ctx)}, why
    if done >= limit:
        why = f"扩展数已达上限（{done}/{limit}），转入汇总"
        return {"decision": "summarize", "reason": why, "emit_ideas": _emit_ideas(ctx)}, why
    why = f"第 {done + 1}/{limit} 轮扩展：当前最优 open 节点 {best.id}（score={best.score}）"
    return {"decision": "expand", "next_round": done + 1}, why


async def _root_of(session, run_id) -> HypothesisNode | None:
    return (
        await session.execute(
            select(HypothesisNode).where(
                HypothesisNode.run_id == run_id, HypothesisNode.parent_id.is_(None)
            )
        )
    ).scalar_one_or_none()


def _as_uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return uuid.UUID(int=0)  # 必然查不到 → 走「引用无效」跳过路径


# ---- 动作 ----


@register("hypothesis.seed")
async def hypothesis_seed(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """树空 → 按研究方向生成根假设。重放（断点恢复后重跑本步）时树已有根：
    不再调 LLM、直接复用——单根不变量在服务层是硬约束，重复建根必炸。"""
    direction = _direction(ctx)
    if not direction:
        raise ValueError("discovery 任务缺少研究方向（params.direction）")
    usage: dict[str, int] = {}
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, ctx.run.id)
        root = await _root_of(session, ctx.run.id)
        if root is None:
            # 结构化调用：模型调用失败抛带 transient 的 LLMError，由引擎决定重试还是停下
            seeded = await ctx.llm.complete_json(
                _STAGE,
                [
                    Message(role="system", content=SEED_SYSTEM_PROMPT),
                    Message(role="user", content=f"研究方向：{direction}"),
                ],
                schema=SeedOut,
                user_id=ctx.run.created_by,
                project_id=ctx.run.project_id,
                library_id=ctx.run.library_id,
                voyage_id=ctx.run.id,
            )
            usage = {
                "prompt_tokens": int(seeded.usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(seeded.usage.get("completion_tokens", 0) or 0),
            }
            root = await tree_service.create_node(
                session,
                run,
                parent_id=None,
                kind="hypothesis",
                statement=seeded.data.statement,
                score=_clamp_score(seeded.data.score),
            )
            _account_node_usage(ctx, str(root.id), usage)
            why = "树为空：按研究方向生成根假设"
        else:
            why = "断点重放：根假设已存在，复用（不重复调 LLM）"
        await ctx.log(f"根假设：{root.statement[:120]}", level="success")
        signal, signal_why = await _next_signal(session, ctx)
    _record_decision(
        ctx,
        action="hypothesis.seed",
        decision=str(signal["decision"]),
        node_id=str(root.id),
        why=f"{why}；{signal_why}",
        round_no=0,
    )
    return {
        "node_id": str(root.id),
        "statement": root.statement,
        "plan_signal": signal,
        "usage": usage,
    }


async def _apply_prune(session, ctx: ActionContext, entry: dict[str, Any]) -> str | None:
    """按 LLM 建议剪一个分支：写 score、级联转 pruned（服务层保证留痕不删）。

    引用非法（跨 run / 不存在 / 已是终态）时跳过并留日志——剪枝建议只是建议，
    不因为一条坏引用打死整个扩展步骤。返回被剪节点 id（跳过返回 None）。
    """
    node = await session.get(HypothesisNode, _as_uuid(entry["node_id"]))
    if node is None or node.run_id != ctx.run.id:
        await ctx.log(f"剪枝建议引用无效节点 {entry['node_id']}，跳过")
        return None
    if node.status in ("pruned", "validated", "refuted"):
        return None
    if entry.get("score") is not None:
        await tree_service.set_score(session, node, entry["score"])
    await tree_service.transition(session, node, "pruned")
    _record_decision(
        ctx,
        action="hypothesis.prune",
        decision="pruned",
        node_id=str(node.id),
        why=str(entry.get("reason") or "LLM 判定该分支不值得继续"),
    )
    await ctx.log(f"剪枝：{node.statement[:80]}（{entry.get('reason') or ''}）")
    return str(node.id)


@register("hypothesis.expand")
async def hypothesis_expand(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """对当前最优 open 节点扩展一轮（#648 起走四段管线）：generate 产最多 4 个
    子候选，逐个 ground → novelty → feasibility → score 全部算完后一次性落库，
    低于 pipeline.MIN_VIABLE_SCORE 的子节点当场剪枝（级联、留痕不删），父节点
    转 expanded。

    恢复语义（确定性重建 = checkpoint 轮次账本 + best_open_node）：
    - 本轮已在账本里 → 纯重放，不再动树；
    - 账本没有、但树上 expanded 数已覆盖本轮（worker 在子节点落库之后、
      checkpoint 回写之前被杀）→ 补记账本、不重复扩；
    - 否则正常扩展：目标节点取 best_open_node（score 降序、同分取先建的，
      对同一棵树是确定性的，所以「杀在扩展之前」的重启会选中同一个节点）。
      管线的全部 LLM/检索都发生在动树**之前**（先算后写）：被杀在管线中途时
    树还没动，重启就是干净重跑，「落库了一半」的窗口没有因为管线变长而变宽。

    锦标赛（params.tournament=True，#653）跑在本轮树落库**之后**：它只改分数
    （set_score 幂等），不建删节点，被杀窗口不因它变宽。已知降级：杀在锦标赛
    中途时，重启走「树上扩展数已覆盖本轮」的补记路径、该轮锦标赛不补跑——
    参赛节点保持 pipeline 分，排序退化为基础模式，宁可少赛一轮也不重复扣费。
    """
    round_no = int(params.get("round") or 0)
    state = _state(ctx)
    usage: dict[str, int] = {}
    children_ids: list[str] = []
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, ctx.run.id)
        record = state["rounds"].get(str(round_no))
        if isinstance(record, dict):
            target_id = record.get("node_id")
            why = f"断点重放：第 {round_no} 轮已在账本中，不再动树"
        elif await _expanded_count(session, ctx.run.id) >= round_no:
            # 子节点已落库但 checkpoint 没记上（杀在两次提交之间）：补记不重扩
            target_id = None
            state["rounds"][str(round_no)] = {"node_id": None, "replayed_from_tree": True}
            why = f"断点重放：树上扩展数已覆盖第 {round_no} 轮，补记账本、不重复扩"
        else:
            target = await tree_service.best_open_node(session, ctx.run.id)
            if target is None:
                # 全被剪光等极端情况：本轮无事可做，交给信号转汇总
                target_id = None
                state["rounds"][str(round_no)] = {"node_id": None, "no_open": True}
                why = f"第 {round_no} 轮无 open 节点可扩展"
            else:
                library_id = _require_library_id(ctx)
                identity: dict[str, Any] = {
                    "user_id": ctx.run.created_by,
                    "project_id": ctx.run.project_id,
                    "voyage_id": ctx.run.id,
                }
                # 方向语境 = run 方向（灵感按它取材，每轮相同、读缓存）+ 被扩展节点的
                # 陈述（focus：候选要围绕这个分支细化）。树上已有的假设一并交给模型，
                # 要求新候选与它们有实质区别——否则细化一个父假设拿回来的常常只是它的
                # 几种改写
                tree_now = await tree_service.tree_for_run(session, ctx.run.id)
                gen = await pipeline.generate(
                    session,
                    ctx.llm,
                    library_id=library_id,
                    direction=_direction(ctx),
                    focus=target.statement,
                    n_candidates=_MAX_CHILDREN,
                    existing=[n.statement for n in tree_now if n.kind == "hypothesis"],
                    **identity,
                )
                candidates = gen["candidates"][:_MAX_CHILDREN]
                if not candidates:
                    raise ValueError("hyp_generate 未产出任何候选假设（去重后为空）")
                usage = dict(gen["usage"])
                # 检索留痕（D6 披露 #655）：本轮发出的全部查询按阶段收进轮次账本。
                # generate 的查询归到被扩展的父节点头上（子节点还没出生）
                round_queries: list[dict[str, Any]] = [
                    {"phase": "generate", "node_id": str(target.id), **q}
                    for q in gen["trace"]["queries"]
                ]
                # 先算后写：每个候选把 ground/novelty/feasibility/score 全部算完，
                # 再统一落库——管线再长，树的被杀窗口也不随之变宽（见函数 docstring）。
                # 候选之间并发（限流），接地 / 查新的查询此刻还没有节点 id 可挂，
                # 落库后再补上归属
                computed = await pipeline.evaluate_candidates(
                    session,
                    ctx.llm,
                    library_id=library_id,
                    statements=[cand["statement"] for cand in candidates],
                    **identity,
                )
                pruned_children: list[str] = []
                for item in computed:
                    node = await tree_service.create_node(
                        session,
                        run,
                        parent_id=target.id,
                        kind="hypothesis",
                        statement=item["statement"],
                        grounding=item["grounding"],
                        novelty_report=item["novelty_report"],
                        feasibility=item["feasibility"],
                        score=item["score"],
                    )
                    children_ids.append(str(node.id))
                    # 检索留痕补归属：这个子假设的接地/查新查询记它头上
                    for q in item["queries"]:
                        round_queries.append({**q, "node_id": str(node.id)})
                    # 每节点记账：这个子假设的接地/查新/可行性花的 token 记它头上
                    _account_node_usage(ctx, str(node.id), item["usage"])
                    usage["prompt_tokens"] += item["usage"]["prompt_tokens"]
                    usage["completion_tokens"] += item["usage"]["completion_tokens"]
                    if item["score"] < pipeline.MIN_VIABLE_SCORE:
                        # 低分自动剪枝：与 hypothesis.prune 同一状态机语义
                        # （级联、留痕不删），分数与依据都留在节点行里可复查
                        await tree_service.transition(session, node, "pruned")
                        pruned_children.append(str(node.id))
                        _record_decision(
                            ctx,
                            action="hypothesis.prune",
                            decision="pruned",
                            node_id=str(node.id),
                            why=(
                                f"score={item['score']} 低于阈值 "
                                f"{pipeline.MIN_VIABLE_SCORE}，自动剪枝"
                            ),
                            round_no=round_no,
                            prune=pipeline.prune_reason(
                                item["grounding"], item["novelty_report"], item["score"]
                            ),
                        )
                        await ctx.log(
                            f"低分剪枝：{item['statement'][:80]}（score={item['score']}）"
                        )
                await tree_service.transition(session, target, "expanded")
                # generate 的开销记到被扩展的父节点头上（子节点还没出生）
                _account_node_usage(ctx, str(target.id), gen["usage"])
                target_id = str(target.id)
                state["rounds"][str(round_no)] = {
                    "node_id": target_id,
                    "children": children_ids,
                    "pruned": pruned_children,
                    # 检索留痕（D6 披露 #655）：本轮查询全录 + 三路灵感的论文集合
                    "queries": round_queries,
                    "inspiration_paper_ids": gen["trace"]["inspiration_paper_ids"],
                    # 燃料留痕（#670）：本轮 generate 消费了哪些方法卡/概念对/
                    # 缺口条目（全空 = 没有燃料可用），随账本进 D6 披露
                    "fuels": gen["trace"]["fuels"],
                }
                why = (
                    f"第 {round_no} 轮：扩展最优 open 节点，管线产出 "
                    f"{len(children_ids)} 个子假设"
                    + (f"（{len(pruned_children)} 个低分被剪）" if pruned_children else "")
                )
                await ctx.log(
                    f"扩展「{target.statement[:80]}」→ {len(children_ids)} 个子假设"
                    + (f"（低分剪枝 {len(pruned_children)} 个）" if pruned_children else ""),
                    level="success",
                )
                if _tournament_enabled(ctx):
                    # 深度模式：本轮树已落库，跑一场锦标赛给存活假设重新排位。
                    # prior = 各节点首赛留档的 pipeline 分（防止上一轮的混合分
                    # 被当成绝对分再混一次，见 tournament 模块 docstring）
                    t_state = state.setdefault("tournament", {"matches": [], "nodes": {}})
                    outcome = await tournament_service.run_tournament(
                        session,
                        ctx.llm,
                        run=run,
                        nodes=await tree_service.tree_for_run(session, ctx.run.id),
                        prior_pipeline_scores={
                            nid: entry.get("pipeline_score")
                            for nid, entry in t_state["nodes"].items()
                        },
                        round_no=round_no,
                        user_id=ctx.run.created_by,
                        project_id=ctx.run.project_id,
                        library_id=library_id,
                    )
                    # 对阵记录累加、终榜整体覆盖（榜单永远是最新一场之后的排位）
                    t_state["matches"].extend(outcome["matches"])
                    t_state["nodes"].update(outcome["nodes"])
                    state["rounds"][str(round_no)]["tournament_matches"] = len(outcome["matches"])
                    # 记账：总账并入本步 usage，每场的 token 对半记到参赛双方头上
                    for key in ("prompt_tokens", "completion_tokens"):
                        usage[key] += outcome["usage"][key]
                    for nid, node_usage in outcome["node_usage"].items():
                        _account_node_usage(ctx, nid, node_usage)
                    if outcome["matches"]:
                        # 终端播报用大白话（界面不讲「锦标赛」行话，代码标识符除外）
                        await ctx.log(
                            f"深度对比：{len(outcome['nodes'])} 个假设两两比了 "
                            f"{len(outcome['matches'])} 场，score 已按胜率混合更新",
                            level="success",
                        )
        signal, signal_why = await _next_signal(session, ctx)
    _record_decision(
        ctx,
        action="hypothesis.expand",
        decision=str(signal["decision"]),
        node_id=target_id,
        why=f"{why}；{signal_why}",
        round_no=round_no,
    )
    return {
        "round": round_no,
        "node_id": target_id,
        "children": children_ids,
        "plan_signal": signal,
        "usage": usage,
    }


@register("hypothesis.prune")
async def hypothesis_prune(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """独立剪枝动作（计划编辑/后续 D3 可显式调度）：写 score + 级联剪枝。"""
    node_id = str(params.get("node_id") or "")
    if not node_id:
        raise ValueError("hypothesis.prune 需要 params.node_id")
    async with get_sessionmaker()() as session:
        pruned = await _apply_prune(
            session,
            ctx,
            {
                "node_id": node_id,
                "score": _clamp_score(params.get("score")),
                "reason": str(params.get("reason") or ""),
            },
        )
    return {"pruned": pruned, "usage": {}}


@register("discovery.summarize")
async def discovery_summarize(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """整树汇总为**研究方案**产物（#648）：direction + 存活假设（按 score 降序，
    携带 grounding/novelty/feasibility 三类证据卡数据）+ 被剪分支附录（§8.2
    防择优汇报：放弃的路径和原因也是产物的一部分）+ 全节点快照与统计。
    产物写进 checkpoint["artifacts"]，voyage 完成标准查它。同时确定性组装
    披露报告 artifacts["discovery-disclosure.json"]（#655 D6：检索/阅读/剪枝/
    对阵/记账全录，services/discovery_disclosure.py）。"""
    state = _state(ctx)
    async with get_sessionmaker()() as session:
        nodes = await tree_service.tree_for_run(session, ctx.run.id)
    stats: dict[str, int] = {}
    for n in nodes:
        stats[n.status] = stats.get(n.status, 0) + 1
    tree_lines = "\n".join(f"- [{n.status}] {n.statement}（score={n.score}）" for n in nodes)
    result = await ctx.llm.complete(
        _STAGE,
        [
            Message(role="system", content=SUMMARY_SYSTEM_PROMPT),
            Message(
                role="user",
                content=f"研究方向：{_direction(ctx)}\n假设树：\n{tree_lines}",
            ),
        ],
        user_id=ctx.run.created_by,
        project_id=ctx.run.project_id,
        library_id=ctx.run.library_id,
        voyage_id=ctx.run.id,
    )
    usage = dict(result.usage or {})
    # 被剪节点的剪枝原因：从决策留痕反查（显式/自动剪枝都记了 why）；
    # 没记到的（随父级联被剪的子孙）如实标注级联
    prune_reasons = {
        str(d.get("node_id")): str(d.get("why") or "")
        for d in state["decisions"]
        if d.get("decision") == "pruned" and d.get("node_id")
    }
    prune_details = {
        str(d.get("node_id")): d["prune"]
        for d in state["decisions"]
        if d.get("decision") == "pruned" and d.get("node_id") and d.get("prune")
    }
    # 锦标赛终榜（深度模式 #653；基础模式为空 dict，下面所有分支都退化为原行为，
    # golden 链因此逐字节不变）
    standings = (state.get("tournament") or {}).get("nodes") or {}

    def _pipeline_score_of(n: HypothesisNode) -> float:
        """排序破平用的 pipeline 分：参赛节点取首赛留档，其余就是自身 score。"""
        entry = standings.get(str(n.id)) or {}
        if entry.get("pipeline_score") is not None:
            return float(entry["pipeline_score"])
        return n.score if n.score is not None else -1.0

    def _tournament_of(n: HypothesisNode) -> dict[str, Any] | None:
        entry = standings.get(str(n.id))
        if not isinstance(entry, dict):
            return None
        return {"win_rate": entry.get("win_rate"), "matches": entry.get("matches")}

    def _hypothesis_entry(n: HypothesisNode) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": str(n.id),
            "statement": n.statement,
            "status": n.status,
            "score": n.score,
            "grounding": n.grounding,
            "novelty_report": n.novelty_report,
            "feasibility": n.feasibility,
        }
        # 参赛节点才带 tournament 字段：混合分的来路要在方案里说得清（D6 披露）
        tournament = _tournament_of(n)
        if tournament is not None:
            entry["tournament"] = tournament
        return entry

    alive = sorted(
        (n for n in nodes if n.status != "pruned"),
        # score 降序（未评分的排最后）；混合分同分按 pipeline 分破（绝对证据分
        # 高者靠前——胜率并列时让「文献说了什么」拍板）；再按建立顺序稳定排序
        key=lambda n: (
            -(n.score if n.score is not None else -1.0),
            -_pipeline_score_of(n),
            n.created_at,
        ),
    )
    artifact = {
        "direction": _direction(ctx),
        "summary": result.content,
        "stats": stats,
        # 研究方案主体：存活假设 + 三类证据卡数据（支持/反驳/推测的 grounding、
        # 逐子命题查新结论、可行性信号与风险论证），前端/导出直接消费
        "hypotheses": [_hypothesis_entry(n) for n in alive],
        # 附录：被剪分支及原因（留痕不删的消费端）
        "pruned_appendix": [
            {
                "id": str(n.id),
                "statement": n.statement,
                "score": n.score,
                "reason": prune_reasons.get(str(n.id), "随父分支级联剪枝"),
                "prune": prune_details.get(str(n.id)),
            }
            for n in nodes
            if n.status == "pruned"
        ],
        "nodes": [
            {
                "id": str(n.id),
                "parent_id": str(n.parent_id) if n.parent_id else None,
                "kind": n.kind,
                "statement": n.statement,
                "status": n.status,
                "score": n.score,
                # 每节点 token 账本随产物归档（记账在 checkpoint，产物是快照）
                "usage": state["node_usage"].get(str(n.id)),
            }
            for n in nodes
        ],
    }
    artifacts = dict(ctx.checkpoint.get("artifacts") or {})
    artifacts["discovery-summary.json"] = json.dumps(artifact, ensure_ascii=False)
    # 披露报告（#655，§8.2）：与研究方案同刻落盘。纯确定性组装（零 LLM），
    # 数据源是 ctx.checkpoint（run.checkpoint 此刻还是旧账，engine 结束后才回写）
    disclosure = discovery_disclosure.build_disclosure(ctx.checkpoint, nodes)
    artifacts["discovery-disclosure.json"] = json.dumps(disclosure, ensure_ascii=False)
    ctx.checkpoint["artifacts"] = artifacts
    _record_decision(
        ctx,
        action="discovery.summarize",
        decision="summarized",
        node_id=None,
        why=f"汇总 {len(nodes)} 个节点（{stats}）",
    )
    return {"node_count": len(nodes), "stats": stats, "usage": usage}


# ---- 假设 → 想法 ----

WRITEUP_STAGE = "hyp_writeup"
# 写作是照着已经判定好的证据把四段写清楚，不是开放推理：保持中档（实测「研究与
# 知识工作」中档与高档质量持平，而这里一次输出近四千 token，高档的钱是实打实的）。
WRITEUP_EFFORT = "medium"
# 一次探索最多写这么多个想法。默认 4 轮 × 每轮 4 个子假设最多长出 16 个，用户要的是
# 一份能挑的短名单，不是把整棵树倒给他
MAX_IDEAS = 8
# 叶子不够这个数时，用已经被细化过的父假设补足（它们也有文献支持，只是有了更具体的
# 版本）。少于三个想法的结果让「挑选」这一步失去意义
MIN_IDEAS_TARGET = 3
# 同时在写的草案数（每个一次模型调用）。与一轮扩展的并发上限同一个理由：网关会 503
WRITEUP_CONCURRENCY = 6

WRITEUP_SYSTEM_PROMPT = """\
POLARIS_HYP_WRITEUP
你把一条已经对照文献检验过的研究假设，写成一份研究想法草案。输入是一个 JSON：
- statement：假设陈述；language：全文用哪种语言写（zh 或 en）；
- evidence：有文献依据的子命题。每条有 subclaim、stance（support 支持 / refute 反驳）
  和 papers（title + snippet，原文片段）；
- unsupported：没有任何文献依据的子命题——只是推测；
- novelty：逐条子命题的查新结论（known 已有 / novel 未见 / uncertain 说不准）。
  查新只对照了用户自己的文献库，没有查库外的工作；
- signals：确定性统计（有支持的子命题占比、新颖子命题占比、依据文献数、库内论文数）；
- risk_note：已有的可行性风险论证。

写作规则：
1. 凡是写成「已有研究表明」「已被证实」的内容，必须能在 evidence 的片段里找到。
   不要补充片段里没有的事实、数字、数据集或文献。
2. unsupported 里的子命题是这个想法里尚待验证的部分：可以写成要检验的假设或实验
   目标，不能写成已有结论，也不要替它们找依据。
3. evidence 里立场为 refute 的内容必须写进 risks。
4. experiments 写怎样检验这条假设（重点是 unsupported 的部分），不要编造结果。
5. scores 四项都是 0–10 的整数。novelty 只能依据 novelty 与 signals 判断；
   feasibility 参考 signals 与 risk_note。
6. title 不超过 80 个字符，不要以「想法：」之类的前缀开头。
7. rationale 的四段直接写给研究者看：不要出现输入里的字段名（support_ratio、
   novel_ratio、grounded_paper_count、risk_note、signals、unsupported 之类），不要写
   「signals 显示」「根据 risk_note」这类说法；要用数字就说成自然的话（如「6 条子命题
   里有 4 条找到了文献支持」）。界面上已经注明新颖性只对照了用户的文献库，rationale
   里不要再写这一句。
"""


class IdeaScoresOut(BaseModel):
    novelty: int
    feasibility: int
    operability: int
    impact: int


class IdeaRationaleOut(BaseModel):
    novelty: str
    feasibility: str
    operability: str
    impact: str


class IdeaWriteup(BaseModel):
    title: pipeline.NonEmptyStr
    motivation: str
    method: str
    experiments: str
    risks: str
    scores: IdeaScoresOut
    rationale: IdeaRationaleOut


_CJK_RE = re.compile(r"[一-鿿]")
#: 「新颖性只对照你的文献库」这句说明：界面凡是显示新颖性分数的地方都按
#: ``origin.novelty_scope`` 加了，理由里再写一遍就是重复。提示里已经让模型别写，
#: 写了也在这里删掉：括号里的整个括号删，自成一句的整句删。
_PAREN_RE = re.compile(r"[（(][^（）()]*[）)]")
#: 按句切开（每个字符都落在某一块里，拼回去与原文一致）
_SENTENCE_PIECE_RE = re.compile(r"[^。！？.!?\n]*[。！？.!?]+|[^。！？.!?\n]+|\n")


def _is_scope_caveat(text: str) -> bool:
    lowered = text.lower()
    if "文献库" in text:
        return any(w in text for w in ("只", "仅")) and any(
            w in text for w in ("对照", "对比", "比对", "基于", "针对", "检索", "查")
        )
    if "library" in lowered:
        return any(w in lowered for w in ("only", "solely")) and any(
            w in lowered for w in ("your", "user")
        )
    return False


def strip_scope_caveat(text: str) -> str:
    """去掉理由里重复界面说明「新颖性只对照你的文献库」的那句话或那个括号。"""
    text = _PAREN_RE.sub(lambda m: "" if _is_scope_caveat(m.group(0)) else m.group(0), text)
    pieces = [p for p in _SENTENCE_PIECE_RE.findall(text) if not _is_scope_caveat(p)]
    return re.sub(r"[ \t]{2,}", " ", "".join(pieces)).strip()


_UNSUPPORTED_HEADING = {
    "zh": ("尚无文献依据的部分", "（每条子命题都在文献库里找到了依据。）"),
    "en": (
        "Not yet supported by the literature",
        "(Every sub-claim has supporting evidence in the library.)",
    ),
}
_SECTION_HEADINGS = {
    "zh": (
        ("动机", "motivation"),
        ("方法概述", "method"),
        ("预期实验", "experiments"),
        ("风险", "risks"),
    ),
    "en": (
        ("Motivation", "motivation"),
        ("Method", "method"),
        ("Experiments", "experiments"),
        ("Risks", "risks"),
    ),
}
_SECTION_PLACEHOLDER = {"zh": "（待补充）", "en": "(to be written)"}


def _idea_language(ctx: ActionContext) -> str:
    """想法用哪种语言写：创建时指定的优先，否则看研究方向里有没有中文。"""
    raw = str(_params_of(ctx).get("language") or "").strip().lower()
    if raw in ("zh", "en"):
        return raw
    return "zh" if _CJK_RE.search(_direction(ctx)) else "en"


def _has_support(node: HypothesisNode) -> bool:
    return any(isinstance(e, dict) and e.get("stance") == "support" for e in (node.grounding or []))


def select_idea_nodes(
    nodes: list[HypothesisNode], *, limit: int = MAX_IDEAS, floor: int = MIN_IDEAS_TARGET
) -> list[HypothesisNode]:
    """哪些假设值得写成想法（纯函数）。

    合格 = 非根的假设节点、没被剪掉、至少一条子命题有文献支持。被剪掉的不写：
    剪枝线（pipeline.MIN_VIABLE_SCORE）已经把「一条新颖的都没有」和「多数子命题
    没有依据」的挡在外面了。根不写：它是探索的起点，没有经过接地和查新。

    优先叶子：一个假设被细化出了合格的子假设，就写更具体的子假设，不写它自己——
    否则同一个想法会以粗细两个版本各出现一次。按分数从高到低取前 ``limit`` 个
    （同分取先建的，与汇总的排序一致）。

    凑不够 ``floor`` 个时，用被跳过的父假设补足：探索很窄（每轮只留下一个子假设）
    的时候，只写叶子会让用户只拿到一两个想法，而树上明明还有带依据的假设。
    """

    def order(n: HypothesisNode) -> tuple[float, int]:
        return (-(n.score if n.score is not None else -1.0), n.seq)

    qualified = [
        n
        for n in nodes
        if n.kind == "hypothesis"
        and n.parent_id is not None
        and n.status != "pruned"
        and _has_support(n)
    ]
    refined = {n.parent_id for n in qualified}
    chosen = sorted((n for n in qualified if n.id not in refined), key=order)[:limit]
    if len(chosen) < floor:
        chosen_ids = {n.id for n in chosen}
        spare = sorted((n for n in qualified if n.id not in chosen_ids), key=order)
        chosen.extend(spare[: floor - len(chosen)])
    return chosen


def _evidence_entries(node: HypothesisNode, titles: dict[str, str]) -> list[dict[str, Any]]:
    """节点的接地结果 → 想法的依据文献：每条（子命题, 论文）一项，立场与原文片段带上。"""
    out: list[dict[str, Any]] = []
    for entry in node.grounding or []:
        if not isinstance(entry, dict) or entry.get("stance") not in ("support", "refute"):
            continue
        paper_ids = [str(p) for p in (entry.get("paper_ids") or [])]
        snippets = [str(x) for x in (entry.get("snippets") or [])]
        owners = [str(x) for x in (entry.get("snippet_paper_ids") or [])]
        by_paper: dict[str, str] = {}
        # 片段在原文里的位置（较新的节点才有）：分段 id + 在分段原文里的 [起, 止)
        chunk_ids = [str(x) for x in (entry.get("snippet_chunk_ids") or [])]
        spans = list(entry.get("snippet_spans") or [])
        located: dict[str, dict[str, Any]] = {}
        if len(owners) == len(snippets):
            for i, (owner, snippet) in enumerate(zip(owners, snippets, strict=True)):
                by_paper.setdefault(owner, snippet)
                if len(chunk_ids) == len(snippets) == len(spans) and owner not in located:
                    located[owner] = {
                        "chunk_id": chunk_ids[i],
                        "match_start": spans[i][0],
                        "match_end": spans[i][1],
                    }
        else:
            # 这个字段出现之前落库的节点：只能按位置对（一篇一段时是对的）
            for owner, snippet in zip(paper_ids, snippets, strict=False):
                by_paper.setdefault(owner, snippet)
        for pid in paper_ids:
            out.append(
                {
                    "paper_id": pid,
                    "title": titles.get(pid, ""),
                    "url": None,
                    "why": str(entry.get("subclaim") or ""),
                    "source": "library",
                    "stance": entry["stance"],
                    "snippet": by_paper.get(pid, ""),
                    **located.get(pid, {}),
                }
            )
    return out


def _unsupported_subclaims(node: HypothesisNode) -> list[str]:
    return [
        str(e.get("subclaim") or "")
        for e in (node.grounding or [])
        if isinstance(e, dict) and e.get("stance") not in ("support", "refute")
    ]


def _ratios(node: HypothesisNode) -> tuple[float, float]:
    """(有支持的子命题占比, 新颖子命题占比)——分数的两个因子，原样给界面看。"""
    grounding = [e for e in (node.grounding or []) if isinstance(e, dict)]
    support = sum(1 for e in grounding if e.get("stance") == "support")
    support_ratio = round(support / len(grounding), 4) if grounding else 0.0
    return support_ratio, round(pipeline.novelty_ratio(node.novelty_report or {}), 4)


def _writeup_payload(
    node: HypothesisNode,
    evidence: list[dict[str, Any]],
    *,
    language: str,
    library_paper_count: int,
) -> dict[str, Any]:
    """写想法那次调用的输入。只放有依据的证据：没有依据的子命题单列在 unsupported，
    模型看不到任何可以拿来替它们圆的材料。"""
    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    for item in evidence:
        grouped.setdefault((item["why"], item["stance"]), []).append(
            {"title": item["title"], "snippet": item["snippet"]}
        )
    support_ratio, novel_ratio = _ratios(node)
    signals = dict(((node.feasibility or {}).get("signals")) or {})
    return {
        "statement": node.statement,
        "language": language,
        "evidence": [
            {"subclaim": subclaim, "stance": stance, "papers": papers}
            for (subclaim, stance), papers in grouped.items()
        ],
        "unsupported": _unsupported_subclaims(node),
        "novelty": [
            {"subclaim": e.get("subclaim"), "verdict": e.get("verdict")}
            for e in ((node.novelty_report or {}).get("subclaims") or [])
            if isinstance(e, dict)
        ],
        "signals": {
            "support_ratio": support_ratio,
            "novel_ratio": novel_ratio,
            "grounded_paper_count": signals.get("grounded_paper_count"),
            "library_paper_count": library_paper_count,
        },
        "risk_note": str((node.feasibility or {}).get("risk_note") or ""),
    }


def _clamp_int_score(raw: Any) -> int:
    try:
        return max(0, min(10, int(raw)))
    except (TypeError, ValueError):
        return 0


def _idea_content(sections: dict[str, Any], unsupported: list[str], language: str) -> str:
    """想法正文：四段 + 末尾单列「尚无文献依据的部分」。

    最后一节不经模型，由代码把没有依据的子命题逐字列出：一句话的假设展开成四段后
    很容易读起来比它的证据更确定，这一节让读的人一眼看到哪些部分还只是推测。
    """
    placeholder = _SECTION_PLACEHOLDER[language]
    parts = [
        f"## {heading}\n\n{str(sections.get(key) or '').strip() or placeholder}"
        for heading, key in _SECTION_HEADINGS[language]
    ]
    heading, none_text = _UNSUPPORTED_HEADING[language]
    body = "\n".join(f"- {s}" for s in unsupported if s.strip()) or none_text
    parts.append(f"## {heading}\n\n{body}")
    return "\n\n".join(parts)


async def _write_up(
    ctx: ActionContext, payload: dict[str, Any], usage: dict[str, int]
) -> IdeaWriteup | None:
    """一个假设一次调用。模型给不出合格的草案（结构两次都不对、写不完、拒答）时
    返回 None——想法照样入库，只是正文待补充：假设和它的证据才是这一步的产物。
    网络、限流、密钥这类失败照常上抛，整步重试，已写好的想法不会重复写。"""
    try:
        result = await pipeline.retry_transient(
            WRITEUP_STAGE,
            lambda: ctx.llm.complete_json(
                WRITEUP_STAGE,
                [
                    Message(role="system", content=WRITEUP_SYSTEM_PROMPT),
                    Message(role="user", content=json.dumps(payload, ensure_ascii=False)),
                ],
                schema=IdeaWriteup,
                effort=WRITEUP_EFFORT,
                user_id=ctx.run.created_by,
                project_id=ctx.run.project_id,
                library_id=ctx.run.library_id,
                voyage_id=ctx.run.id,
            ),
        )
    except (LLMSchemaError, LLMTruncatedError, LLMRefusalError):
        return None
    usage["prompt_tokens"] += int(result.usage.get("prompt_tokens", 0) or 0)
    usage["completion_tokens"] += int(result.usage.get("completion_tokens", 0) or 0)
    return result.data


async def _existing_idea_for_node(session, run_id: uuid.UUID, node_id: str) -> Idea | None:
    """这次任务是否已经为这个节点写过想法（重放 / 恢复时靠它不重复写）。"""
    rows = (
        (await session.execute(select(Idea).where(Idea.source_voyage_id == run_id))).scalars().all()
    )
    for idea in rows:
        if isinstance(idea.origin, dict) and str(idea.origin.get("node_id")) == node_id:
            return idea
    return None


@register("discovery.write_ideas")
async def discovery_write_ideas(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """把留下来的假设写成课题里的想法（选哪些见 :func:`select_idea_nodes`）。

    每个假设：一次模型调用写出标题、四段正文和四维评分 → 与课题里**这次探索之外**
    的想法去重（与想法生成同一套：向量余弦过 0.85 再用重排复核）→ 入库。假设陈述原样作为
    想法的概述，接地时选中的每条（子命题, 论文）连同立场和原文片段进 ``evidence``。

    不夸大（设计 Q6）：写作调用只拿到有文献依据的证据；没有依据的子命题只以
    「尚待验证」的名义给它，并由代码在正文末尾逐字单列一节。新颖性只对照了文献库：
    ``origin.novelty_scope`` 记着 ``library``，界面据此在分数旁说明（理由里不再重复）。

    可重放：每写完一个就提交。再跑这一步时，已经为某个节点写过想法（按
    ``source_voyage_id`` + ``origin.node_id`` 查）就跳过，不再调模型；上次判成重复的
    从 checkpoint 里认出来，同样跳过。
    """
    if ctx.run.project_id is None:
        raise ValueError("假设探索任务没有所属课题，无法写入想法")
    state = _state(ctx)
    ledger = state.setdefault("ideas", {})
    language = _idea_language(ctx)
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    library_id = _require_library_id(ctx)
    async with get_sessionmaker()() as session:
        nodes = await tree_service.tree_for_run(session, ctx.run.id)
        selected = select_idea_nodes(nodes)
        library_paper_count = await voyages_service.indexed_library_paper_count(session, library_id)
        paper_ids = {
            uuid.UUID(str(pid))
            for n in selected
            for e in (n.grounding or [])
            if isinstance(e, dict)
            for pid in (e.get("paper_ids") or [])
        }
        titles: dict[str, str] = {}
        if paper_ids:
            rows = (
                await session.execute(select(Paper.id, Paper.title).where(Paper.id.in_(paper_ids)))
            ).all()
            titles = {str(pid): (title or "") for pid, title in rows}
    written: list[str] = []
    duplicates: list[dict[str, Any]] = []

    # 草案之间互不依赖：先并发把还没写过的全部写出来（限流），再逐个去重、入库。
    # 以前是写一个存一个，7 个想法排队 6 分钟；入库仍然逐个提交，重放语义不变。
    pending: list[HypothesisNode] = []
    async with get_sessionmaker()() as session:
        for node in selected:
            recorded = ledger.get(str(node.id))
            if isinstance(recorded, dict) and recorded.get("duplicate"):
                continue
            if await _existing_idea_for_node(session, ctx.run.id, str(node.id)) is None:
                pending.append(node)
    gate = asyncio.Semaphore(WRITEUP_CONCURRENCY)

    async def draft(node: HypothesisNode) -> IdeaWriteup | None:
        async with gate:
            return await _write_up(
                ctx,
                _writeup_payload(
                    node,
                    _evidence_entries(node, titles),
                    language=language,
                    library_paper_count=library_paper_count,
                ),
                usage,
            )

    results = await asyncio.gather(*(draft(n) for n in pending), return_exceptions=True)
    drafts: dict[str, IdeaWriteup | None] = {}
    failure: BaseException | None = None
    for node, result in zip(pending, results, strict=True):
        if isinstance(result, BaseException):
            # 网络 / 限流这类失败：写成的照样入库，最后再抛，重跑这一步只补没写成的
            failure = failure or result
        else:
            drafts[str(node.id)] = result

    for node in selected:
        node_id = str(node.id)
        async with get_sessionmaker()() as session:
            already = await _existing_idea_for_node(session, ctx.run.id, node_id)
        if already is not None:
            ledger[node_id] = {"idea_id": str(already.id), "duplicate": False}
            written.append(str(already.id))
            continue
        recorded = ledger.get(node_id)
        if isinstance(recorded, dict) and recorded.get("duplicate"):
            duplicates.append({"node_id": node_id, **recorded})
            continue

        if node_id not in drafts:
            continue  # 这一个的草案调用失败了（见上），留给重跑
        evidence = _evidence_entries(node, titles)
        unsupported = _unsupported_subclaims(node)
        writeup = drafts[node_id]
        if writeup is None:
            await ctx.log(f"这条假设的草案没写出来，先只存假设和依据：{node.statement[:60]}")
        title = (writeup.title if writeup else node.statement).strip()[:80] or node.statement[:80]
        scores: dict[str, int] | None = None
        rationale: dict[str, str] | None = None
        if writeup is not None:
            scores = {k: _clamp_int_score(v) for k, v in writeup.scores.model_dump().items()}
            rationale = {
                k: strip_scope_caveat(str(v)) for k, v in writeup.rationale.model_dump().items()
            }
        content = _idea_content(writeup.model_dump() if writeup else {}, unsupported, language)
        support_ratio, novel_ratio = _ratios(node)
        parent_paper_ids = list(dict.fromkeys(item["paper_id"] for item in evidence))
        text = idea_dedup.idea_text(title, node.statement)

        async with get_sessionmaker()() as session:
            vector: list[float] | None = None
            space = None
            try:
                vectors, space = await embed_documents(
                    session,
                    [text],
                    user_id=ctx.run.created_by,
                    project_id=ctx.run.project_id,
                    voyage_id=ctx.run.id,
                )
                vector = vectors[0]
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 嵌入不可用：不去重、不存向量，想法照写
                await session.rollback()
            match = None
            if vector is not None and space is not None:
                # 只和这次探索之外的想法比（以前的探索、想法生成、手写的）。同一次探索里
                # 的假设在生成时已经互相去过重，而且兄弟假设围绕同一个父假设展开，
                # 向量天然很近——再拿它们互相比，一棵树常常只剩一个想法。
                own = {uuid.UUID(i) for i in written}
                existing = [
                    entry
                    for entry in await idea_dedup.existing_idea_entries(
                        session,
                        project_id=ctx.run.project_id,
                        space=space,
                        user_id=ctx.run.created_by,
                        voyage_id=ctx.run.id,
                    )
                    if entry.idea_id not in own
                ]
                match = await idea_dedup.find_duplicate(
                    ctx.llm,
                    text=text,
                    vector=vector,
                    kept=existing,
                    user_id=ctx.run.created_by,
                    project_id=ctx.run.project_id,
                    voyage_id=ctx.run.id,
                )
            if match is not None:
                ledger[node_id] = {
                    "idea_id": str(match.entry.idea_id) if match.entry.idea_id else None,
                    "duplicate": True,
                    "duplicate_of": match.entry.label,
                    "cosine": round(match.cosine, 4),
                }
                duplicates.append({"node_id": node_id, **ledger[node_id]})
                await ctx.log(f"与已有想法重复，未重复写入：{title}")
                continue
            idea = Idea(
                project_id=ctx.run.project_id,
                title=title,
                summary=node.statement,
                content=content,
                scores=scores,
                score_rationale=rationale,
                status="candidate",
                depth="sketch",
                evidence=evidence,
                parent_paper_ids=parent_paper_ids,
                source_voyage_id=ctx.run.id,
                origin={
                    "kind": "discovery",
                    "node_id": node_id,
                    "pipeline_score": node.score,
                    "support_ratio": support_ratio,
                    "novel_ratio": novel_ratio,
                    # 新颖性只对照了这个文献库，没有查库外的工作——界面凡是显示
                    # 新颖性分数的地方都按这个字段加一句说明
                    "novelty_scope": "library",
                    "library_id": str(library_id),
                    "library_paper_count": library_paper_count,
                    "unsupported_subclaims": unsupported,
                },
            )
            session.add(idea)
            await session.flush()
            if vector is not None and space is not None:
                await upsert_idea_vector(session, idea.id, vector, space)
            # 写一个提交一个：后面的假设调模型失败时，前面写好的不丢，重放也不重复写
            await session.commit()
            ledger[node_id] = {"idea_id": str(idea.id), "duplicate": False}
            written.append(str(idea.id))
            await ctx.log(f"写入想法：{title}", level="success")

    if failure is not None:
        raise failure

    async with get_sessionmaker()() as session:
        session.add(
            Activity(
                project_id=ctx.run.project_id,
                actor="agent:discovery",
                kind="discovery.ideas_written",
                message=f"假设探索完成：{len(written)} 个想法入库",
                payload={
                    "voyage_id": str(ctx.run.id),
                    "written": len(written),
                    "duplicates": len(duplicates),
                },
            )
        )
        await session.commit()
    _record_decision(
        ctx,
        action="discovery.write_ideas",
        decision="ideas_written",
        node_id=None,
        why=(
            f"选中 {len(selected)} 个假设：写入 {len(written)} 个想法，"
            f"{len(duplicates)} 个与已有想法重复"
        ),
    )
    return {
        "selected": len(selected),
        "ideas": len(written),
        "duplicates": len(duplicates),
        "idea_ids": written,
        "duplicate_detail": duplicates,
        "usage": usage,
    }
