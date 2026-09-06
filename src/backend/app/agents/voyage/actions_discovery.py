"""discovery 任务的树搜索动作（#642，设计报告 §8.2，P2 D2）。

四个动作围绕假设树（services/hypothesis_tree.py，#640）展开：

- ``hypothesis.seed``：树空时按研究方向生成根假设入树；
- ``hypothesis.expand``：对当前最优 open 节点跑四段假设管线（#648，
  services/hypothesis_pipeline.py）：generate 产 2-3 个子候选，逐个
  ground → novelty → feasibility → score 写进节点，低分自动剪枝；
- ``hypothesis.prune``：显式剪枝入口，写 score 并级联剪枝（留痕不删）；
- ``discovery.summarize``：把整棵树（含被剪分支）汇总成研究方案产物后收束。

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

import json
import uuid
from typing import Any

from sqlalchemy import func, select

from app.agents.voyage.actions import ActionContext, register
from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.models.hypothesis import HypothesisNode
from app.models.voyage import VoyageRun
from app.services import discovery_disclosure
from app.services import hypothesis_pipeline as pipeline
from app.services import hypothesis_tournament as tournament_service
from app.services import hypothesis_tree as tree_service

# LLM 环节：结构化 JSON 生成，走中档耐心（core/llm/router.py 的 _MEDIUM_CALL_STAGES）
_STAGE = "discovery_plan"

_MAX_JSON_ATTEMPTS = 3
# 每轮扩展的子候选上限：管线 generate 按它要候选，多给截断（去重后可能更少）
_MAX_CHILDREN = 3

SEED_SYSTEM_PROMPT = """\
POLARIS_DISCOVERY_SEED
你是 Navigator，负责为一个研究方向提出树搜索的根假设：一条值得展开验证的、
可检验的核心研究假设。只输出一个 JSON 对象，不要输出任何其他文字：
{"statement": "假设陈述（一句话，可检验）", "rationale": "一句话依据", \
"score": 0 到 1 的先验看好程度}
"""

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
        return max(0, int(_params_of(ctx).get("max_expansions", 3)))
    except (TypeError, ValueError):
        return 3


def _tournament_enabled(ctx: ActionContext) -> bool:
    return bool(_params_of(ctx).get("tournament"))


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
) -> None:
    """每轮决策留痕（哪个节点、为什么）：SSE 不持久，恢复与审计都要靠它。"""
    _state(ctx)["decisions"].append(
        {
            "action": action,
            "round": round_no,
            "decision": decision,
            "node_id": node_id,
            "why": why,
        }
    )


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


# ---- LLM JSON 调用（带 usage 回传：每节点记账需要它，_complete_json 不返回） ----


def _extract_json(content: str) -> Any:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(content[start : end + 1])


async def _complete_json_with_usage(
    ctx: ActionContext, *, system: str, user: str, validate
) -> tuple[Any, dict[str, int]]:
    """LLM JSON 请求：解析/校验失败重试；返回 (结果, 累计 usage)。

    重试轮次的 token 也计入 usage——记账要如实反映这一步真实花了多少。
    """
    total = {"prompt_tokens": 0, "completion_tokens": 0}
    last_error: Exception | None = None
    for _attempt in range(_MAX_JSON_ATTEMPTS):
        result = await ctx.llm.complete(
            _STAGE,
            [Message(role="system", content=system), Message(role="user", content=user)],
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            library_id=ctx.run.library_id,
            voyage_id=ctx.run.id,
        )
        usage = result.usage or {}
        total["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
        total["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
        try:
            return validate(_extract_json(result.content)), total
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            last_error = e
    raise ValueError(f"LLM 连续输出非法 JSON：{last_error}")


def _clamp_score(raw: Any) -> float | None:
    try:
        return min(1.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return None


def _validate_seed(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not str(data.get("statement") or "").strip():
        raise ValueError('seed 输出需含非空 "statement"')
    return {
        "statement": str(data["statement"]).strip(),
        "score": _clamp_score(data.get("score")),
    }


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
        return {"decision": "summarize", "reason": why}, why
    if done >= limit:
        why = f"扩展数已达上限（{done}/{limit}），转入汇总"
        return {"decision": "summarize", "reason": why}, why
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
            seed, usage = await _complete_json_with_usage(
                ctx,
                system=SEED_SYSTEM_PROMPT,
                user=f"研究方向：{direction}",
                validate=_validate_seed,
            )
            root = await tree_service.create_node(
                session,
                run,
                parent_id=None,
                kind="hypothesis",
                statement=seed["statement"],
                score=seed["score"],
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
    """对当前最优 open 节点扩展一轮（#648 起走四段管线）：generate 产 2-3 个
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
                # 方向语境 = run 方向 + 被扩展节点的陈述：候选要围绕这个分支细化
                gen = await pipeline.generate(
                    session,
                    ctx.llm,
                    library_id=library_id,
                    direction=f"{_direction(ctx)}；父假设：{target.statement}",
                    n_candidates=_MAX_CHILDREN,
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
                # 再统一落库——管线再长，树的被杀窗口也不随之变宽（见函数 docstring）
                computed: list[dict[str, Any]] = []
                for cand in candidates:
                    grounded = await pipeline.ground(
                        session,
                        ctx.llm,
                        library_id=library_id,
                        statement=cand["statement"],
                        **identity,
                    )
                    nov = await pipeline.novelty(
                        session,
                        ctx.llm,
                        library_id=library_id,
                        statement=cand["statement"],
                        grounding=grounded["grounding"],
                        **identity,
                    )
                    feas = await pipeline.feasibility(
                        session,
                        ctx.llm,
                        library_id=library_id,
                        statement=cand["statement"],
                        grounding=grounded["grounding"],
                        **identity,
                    )
                    child_usage = {"prompt_tokens": 0, "completion_tokens": 0}
                    for part in (grounded, nov, feas):
                        child_usage["prompt_tokens"] += int(
                            part["usage"].get("prompt_tokens", 0) or 0
                        )
                        child_usage["completion_tokens"] += int(
                            part["usage"].get("completion_tokens", 0) or 0
                        )
                    computed.append(
                        {
                            "statement": cand["statement"],
                            "grounding": grounded["grounding"],
                            "novelty_report": nov["report"],
                            "feasibility": feas["feasibility"],
                            "score": pipeline.score_hypothesis(
                                grounded["grounding"], nov["report"]
                            ),
                            "usage": child_usage,
                            # 接地/查新的查询此刻还没有节点 id 可挂（先算后写），
                            # 落库后再补上归属
                            "queries": [
                                {"phase": "ground", **q}
                                for q in grounded["trace"]["queries"]
                            ]
                            + [
                                {"phase": "novelty", **q}
                                for q in nov["trace"]["queries"]
                            ],
                        }
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
                    state["rounds"][str(round_no)]["tournament_matches"] = len(
                        outcome["matches"]
                    )
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
