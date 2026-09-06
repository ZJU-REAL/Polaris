"""假设锦标赛深度模式（#653，设计报告 §12，P2 D4；不 import fastapi）。

四段管线的 score（novel 比例 × support 覆盖率，hypothesis_pipeline.score_hypothesis）
是逐假设独立算的绝对分：可解释，但分不出「两个都及格的假设哪个更值得先做」。
锦标赛补的就是这半边——把当前最有希望的假设**两两对比**（co-scientist 的
Elo 排位思路取最小实现：单轮 round-robin，不做多轮淘汰），让相对判断参与排序：

- 参赛者 = pipeline score 前 ``MAX_CONTENDERS`` 的 open|expanded 假设节点。
  上限卡死配对数（C(6,2)=15 对），深度模式的加时成本随树增长有界；
- 每对打一场 ``hyp_compare``（短档 JSON 判定）：给裁判两假设的陈述 +
  接地立场摘要 + 查新结论，判 winner（a|b|tie）+ 一句 rationale。
  **不给裁判看 pipeline score**——相对判断要独立于绝对分，否则只是复读；
- win-rate = 胜场 / 参赛场（tie 各记半场）；
- 新 score = 0.5 × pipeline score + 0.5 × win-rate。对半开是有意的：绝对分
  （证据说了什么）与相对分（裁判怎么排）谁也不该压倒谁，权重调优留给后续
  里程碑。同分排序由消费端（discovery.summarize）按 pipeline score 破。

pipeline score 的口径：节点**首次参赛前**的 score。多轮扩展下节点会反复参赛，
若每轮都拿「当前分」当 pipeline 分，上一轮的 win-rate 会被再次混入、绝对分被
逐轮稀释——所以调用方要把首赛留档的 pipeline_score（prior）传回来。

比较记录（对阵、判决、rationale）由调用方写进 checkpoint 轮次账本，供 D6 披露。
"""

import uuid
from itertools import combinations
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.router import LLMRouter
from app.models.hypothesis import HypothesisNode
from app.models.voyage import VoyageRun
from app.services import hypothesis_tree as tree_service

# 与管线四环节共用同一套 JSON 调用约定（解析重试 + usage 如实归并），刻意不再抄一份
from app.services.hypothesis_pipeline import _complete_json

# LLM 环节（router.py STAGES / 前端 LLM_STAGES 同步登记）：短 JSON 判定走短档
COMPARE_STAGE = "hyp_compare"

# 参赛上限：round-robin 配对数是 C(n,2)，6 人封顶 15 场——深度模式的加时成本
# 必须有界，且排位靠后的假设本来就不在「谁先做」的争议区里
MAX_CONTENDERS = 6
# pipeline 绝对分与锦标赛相对分的混合权重（见模块 docstring 的 why）
PIPELINE_WEIGHT = 0.5

COMPARE_SYSTEM_PROMPT = """\
POLARIS_HYP_COMPARE
你是研究假设对比裁判。输入 JSON 里是两个假设（a / b），各带陈述、逐子命题的
文献接地立场（support/refute/speculation）与查新结论（known/novel/uncertain）。
判断哪个更值得优先投入验证——综合新颖性、证据扎实度与可检验性；难分高下时
如实判平局。只输出 JSON：{"winner": "a|b|tie", "rationale": "一句话理由"}"""


def pick_contenders(nodes: list[HypothesisNode]) -> list[HypothesisNode]:
    """选参赛者：open|expanded 的假设节点按 pipeline score 前 MAX_CONTENDERS。

    expanded 也参赛：被扩展只说明它探过了，不说明它不如新生的子假设——
    排序面向「整棵树里哪些假设最值得写进方案」，不是只排叶子。
    终态（pruned/validated/refuted）不参赛：结论已定，无需再排。
    同分按 created_at 取先建的（与 best_open_node 同一确定性口径）。
    """
    alive = [n for n in nodes if n.kind == "hypothesis" and n.status in ("open", "expanded")]
    alive.sort(key=lambda n: (-(n.score if n.score is not None else -1.0), n.created_at))
    return alive[:MAX_CONTENDERS]


def _brief(node: HypothesisNode) -> dict[str, Any]:
    """裁判可见的假设摘要：陈述 + 接地立场 + 查新结论，**不含分数**（独立判断）。"""
    grounding = node.grounding if isinstance(node.grounding, list) else []
    subclaims = (node.novelty_report or {}).get("subclaims") or []
    return {
        "statement": node.statement,
        "grounding": [
            {"subclaim": g.get("subclaim"), "stance": g.get("stance")}
            for g in grounding
            if isinstance(g, dict)
        ],
        "novelty": [
            {"subclaim": e.get("subclaim"), "verdict": e.get("verdict")}
            for e in subclaims
            if isinstance(e, dict)
        ],
    }


def _validate_compare(data: Any) -> dict[str, str]:
    if not isinstance(data, dict) or data.get("winner") not in ("a", "b", "tie"):
        raise ValueError('compare 输出需含 "winner"（a|b|tie）')
    return {
        "winner": str(data["winner"]),
        "rationale": str(data.get("rationale") or "").strip(),
    }


def blend_score(pipeline_score: float | None, win_rate: float) -> float:
    """新 score = 0.5 × pipeline + 0.5 × win-rate；pipeline 缺失按 0 记（诚实缺省：
    没被管线评过分的假设不该白得绝对分的一半）。"""
    base = pipeline_score if pipeline_score is not None else 0.0
    return round(PIPELINE_WEIGHT * base + (1 - PIPELINE_WEIGHT) * win_rate, 4)


async def run_tournament(
    session: AsyncSession,
    llm: LLMRouter,
    *,
    run: VoyageRun,
    nodes: list[HypothesisNode],
    prior_pipeline_scores: dict[str, Any] | None = None,
    round_no: int | None = None,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID,
) -> dict[str, Any]:
    """single round-robin：选参赛者 → 逐对裁判 → win-rate 混分 → set_score 落库。

    返回::

        {
          "matches":  [{round, a, b, winner, rationale}, ...],  # 对阵全记录
          "nodes":    {node_id: {win_rate, matches, pipeline_score, score}},  # 终榜
          "node_usage": {node_id: {prompt_tokens, completion_tokens}},  # 每节点分摊
          "usage":    {prompt_tokens, completion_tokens},  # 总账
        }

    参赛不足 2 人时是无事发生的空结果（不动树、不调 LLM）。
    ``prior_pipeline_scores``：往轮首赛留档的 pipeline 分（{node_id: score}，
    见模块 docstring——防止上一轮的混合分被当成绝对分再混一次）。
    """
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    contenders = pick_contenders(nodes)
    if len(contenders) < 2:
        return {"matches": [], "nodes": {}, "node_usage": {}, "usage": usage}
    prior = prior_pipeline_scores or {}
    # 参赛者的 pipeline 分：首赛用当前分留档，往轮参赛者沿用首赛留档
    pipeline_scores: dict[str, float | None] = {
        str(n.id): prior.get(str(n.id), n.score) for n in contenders
    }
    matches: list[dict[str, Any]] = []
    node_usage: dict[str, dict[str, int]] = {
        str(n.id): {"prompt_tokens": 0, "completion_tokens": 0} for n in contenders
    }
    # 胜场按 tie 各半记：用「半场 = 1」的整数计数（wins2）避免浮点累加误差
    wins2: dict[str, int] = {str(n.id): 0 for n in contenders}
    games: dict[str, int] = {str(n.id): 0 for n in contenders}
    for a, b in combinations(contenders, 2):
        match_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        verdict = await _complete_json(
            llm,
            COMPARE_STAGE,
            system=COMPARE_SYSTEM_PROMPT,
            payload={"a": _brief(a), "b": _brief(b)},
            validate=_validate_compare,
            usage=match_usage,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=run.id,
        )
        id_a, id_b = str(a.id), str(b.id)
        games[id_a] += 1
        games[id_b] += 1
        if verdict["winner"] == "a":
            wins2[id_a] += 2
        elif verdict["winner"] == "b":
            wins2[id_b] += 2
        else:
            wins2[id_a] += 1
            wins2[id_b] += 1
        matches.append(
            {
                "round": round_no,
                "a": id_a,
                "b": id_b,
                "winner": verdict["winner"],
                "rationale": verdict["rationale"],
            }
        )
        # 每节点记账：一场对比是两个假设共同消费的，token 对半分摊
        # （零头记给 a——确定性分法，总量守恒）
        for key in ("prompt_tokens", "completion_tokens"):
            usage[key] += match_usage[key]
            node_usage[id_b][key] += match_usage[key] // 2
            node_usage[id_a][key] += match_usage[key] - match_usage[key] // 2
    standings: dict[str, dict[str, Any]] = {}
    for node in contenders:
        node_id = str(node.id)
        win_rate = round(wins2[node_id] / (2 * games[node_id]), 4)
        blended = blend_score(pipeline_scores[node_id], win_rate)
        standings[node_id] = {
            "win_rate": win_rate,
            "matches": games[node_id],
            "pipeline_score": pipeline_scores[node_id],
            "score": blended,
        }
        # 混合分直接写回节点 score：best_open_node 的下一轮选点与 summarize 的
        # 方案排序都读它——锦标赛的意义就是改变后续决策，不是只留一张榜
        await tree_service.set_score(session, node, blended)
    return {"matches": matches, "nodes": standings, "node_usage": node_usage, "usage": usage}
