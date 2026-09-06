"""假设锦标赛深度模式（#653 D4）：round-robin 对比 → win-rate 混分重排。

- fake provider 的对比替身规则是「陈述字典序小者胜、同串判 tie」——完全确定性，
  所以开/关锦标赛的排序差异、win-rate 与混合分都能精确断言；
- 服务层：参赛上限（>6 只取 score 前 6、C(6,2)=15 场）、状态/kind 过滤、tie 各记
  半场、不足 2 人无事发生、pipeline 分首赛留档（prior 防止混合分被二次混合）;
- 端到端：tournament=True 的 run 每轮扩展后重排 score、比较记录进轮次账本、
  节点记账含锦标赛 token、汇总产物给参赛节点带 tournament 字段、披露端点可读；
  tournament=False 的 run 与 #648 行为逐项一致（golden 链也钉着这点）。
"""

import json
import uuid

from sqlalchemy import select

from app.agents.voyage.engine import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.hypothesis import HypothesisNode
from app.models.voyage import VoyageRun
from app.services import hypothesis_tournament as tournament
from app.services import hypothesis_tree as tree_service
from tests.conftest import RecordingBus
from tests.test_voyage_discovery import _make_workspace, _tree

# ---- 服务层（直接驱动 run_tournament，树用 tree_service 手搭） ----


async def _bare_run(session) -> VoyageRun:
    """最小 discovery run（服务层测试不走引擎，只要 run_id 挂树）。"""
    run = VoyageRun(kind="discovery", goal="tournament-service", status="executing", cursor=0)
    session.add(run)
    await session.commit()
    return run


async def test_tournament_caps_contenders_and_filters(client):
    """9 个节点：pruned/experiment 不参赛，score 前 6 参赛打满 15 场，
    榜外节点分数原封不动。fake 规则下（"h1" < "h2" < …）战绩完全可预期。"""
    async with get_sessionmaker()() as session:
        run = await _bare_run(session)
        root = await tree_service.create_node(
            session, run, parent_id=None, kind="hypothesis", statement="h-root", score=0.05
        )
        children = []
        for i, score in enumerate([0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2]):
            children.append(
                await tree_service.create_node(
                    session,
                    run,
                    parent_id=root.id,
                    kind="hypothesis",
                    statement=f"h{i + 1}",
                    score=score,
                )
            )
        # 高分但已剪枝 / 非假设 kind：都不该入场
        pruned = await tree_service.create_node(
            session, run, parent_id=root.id, kind="hypothesis", statement="h0-pruned", score=0.95
        )
        await tree_service.transition(session, pruned, "pruned")
        experiment = await tree_service.create_node(
            session, run, parent_id=root.id, kind="experiment", statement="h0-exp", score=0.99
        )
        nodes = await tree_service.tree_for_run(session, run.id)
        result = await tournament.run_tournament(
            session, LLMRouter(), run=run, nodes=nodes, library_id=uuid.uuid4()
        )

        # 参赛者 = score 前 6 的子节点；root(0.05)/h7(0.2) 落榜，pruned/experiment 出局
        contender_ids = {str(c.id) for c in children[:6]}
        assert set(result["nodes"]) == contender_ids
        assert len(result["matches"]) == 15  # C(6,2)
        assert all(m["winner"] in ("a", "b") for m in result["matches"])
        # 字典序：h1 全胜 → 胜率 1.0、0.8、0.6、0.4、0.2、0.0 依次递减
        expected = {
            "h1": (1.0, 0.9),  # 0.5×0.8 + 0.5×1.0
            "h2": (0.8, 0.75),
            "h3": (0.6, 0.6),
            "h4": (0.4, 0.45),
            "h5": (0.2, 0.3),
            "h6": (0.0, 0.15),
        }
        for child in children[:6]:
            entry = result["nodes"][str(child.id)]
            win_rate, blended = expected[child.statement]
            assert entry["win_rate"] == win_rate
            assert entry["score"] == blended
            assert entry["matches"] == 5
            await session.refresh(child)
            assert child.score == blended  # set_score 已落库
        # 榜外与出局节点：分数原封不动
        for node, score in ((children[6], 0.2), (root, 0.05), (pruned, 0.95), (experiment, 0.99)):
            await session.refresh(node)
            assert node.score == score
        # 记账：每场 token 对半分摊，参赛者人人有份、总量守恒
        assert set(result["node_usage"]) == contender_ids
        assert all(u["prompt_tokens"] > 0 for u in result["node_usage"].values())
        for key in ("prompt_tokens", "completion_tokens"):
            assert sum(u[key] for u in result["node_usage"].values()) == result["usage"][key]


async def test_tournament_tie_and_prior_pipeline_scores(client):
    """同串陈述判 tie（各记半场）；prior 让第二次锦标赛仍从原始 pipeline 分混合，
    而不是把上一轮的混合分再混一次。"""
    async with get_sessionmaker()() as session:
        run = await _bare_run(session)
        a = await tree_service.create_node(
            session, run, parent_id=None, kind="hypothesis", statement="same-claim", score=0.4
        )
        b = await tree_service.create_node(
            session, run, parent_id=a.id, kind="hypothesis", statement="same-claim", score=0.2
        )
        nodes = await tree_service.tree_for_run(session, run.id)
        first = await tournament.run_tournament(
            session, LLMRouter(), run=run, nodes=nodes, library_id=uuid.uuid4()
        )
        assert [m["winner"] for m in first["matches"]] == ["tie"]
        assert first["nodes"][str(a.id)] == {
            "win_rate": 0.5,
            "matches": 1,
            "pipeline_score": 0.4,
            "score": 0.45,  # 0.5×0.4 + 0.5×0.5
        }
        assert first["nodes"][str(b.id)]["score"] == 0.35
        # 第二次带 prior：pipeline 分沿用首赛留档（0.4/0.2），结果稳定不漂移；
        # 若错拿当前混合分（0.45/0.35）再混，分数会被逐轮稀释
        nodes = await tree_service.tree_for_run(session, run.id)
        second = await tournament.run_tournament(
            session,
            LLMRouter(),
            run=run,
            nodes=nodes,
            prior_pipeline_scores={
                nid: entry["pipeline_score"] for nid, entry in first["nodes"].items()
            },
            library_id=uuid.uuid4(),
        )
        assert second["nodes"] == first["nodes"]


async def test_tournament_needs_two_contenders(client):
    """只有一个存活假设：无对手，无事发生（不调 LLM、不改分）。"""
    async with get_sessionmaker()() as session:
        run = await _bare_run(session)
        root = await tree_service.create_node(
            session, run, parent_id=None, kind="hypothesis", statement="lonely", score=0.6
        )
        result = await tournament.run_tournament(
            session,
            LLMRouter(),
            run=run,
            nodes=await tree_service.tree_for_run(session, run.id),
            library_id=uuid.uuid4(),
        )
        assert result == {
            "matches": [],
            "nodes": {},
            "node_usage": {},
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
        await session.refresh(root)
        assert root.score == 0.6


def test_blend_score_treats_missing_pipeline_as_zero():
    assert tournament.blend_score(None, 1.0) == 0.5
    assert tournament.blend_score(0.8, 0.0) == 0.4


# ---- 端到端：开 vs 关（fake 确定性下排序差异可精确断言） ----


async def _create_run_via_api(
    client, headers, project_id: str, library_id: uuid.UUID, *, tournament_on: bool
) -> uuid.UUID:
    resp = await client.post(
        "/api/voyages",
        json={
            "kind": "discovery",
            "project_id": project_id,
            "goal": "可验证的 agent 工作流",
            "params": {
                "direction": "可验证的 agent 工作流",
                "library_id": str(library_id),
                "max_expansions": 1,
                "tournament": tournament_on,
            },
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _node_usage_of(run_id: uuid.UUID) -> dict:
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        return (run.checkpoint or {})["discovery"]["node_usage"]


async def test_tournament_run_reranks_vs_off(client, queue_stub):
    """同一工作区各跑一遍开/关：关 = #648 原行为；开 = 三个存活假设打 3 场，
    fake 字典序（围 < 机 < 证）下 root 全胜、fake A 半胜、fake B 全败，
    score 从 [0.8, 0.25, 0.25] 重排为 [0.9, 0.375, 0.125]。"""
    project_id, library_id, headers = await _make_workspace(client)
    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())

    run_off = await _create_run_via_api(
        client, headers, project_id, library_id, tournament_on=False
    )
    run_on = await _create_run_via_api(client, headers, project_id, library_id, tournament_on=True)
    await engine.run(run_off)
    await engine.run(run_on)

    def _pick(nodes: list[HypothesisNode]) -> tuple[HypothesisNode, HypothesisNode, HypothesisNode]:
        root = next(n for n in nodes if n.parent_id is None)
        child_a = next(n for n in nodes if "fake A" in n.statement)
        child_b = next(n for n in nodes if "fake B" in n.statement)
        return root, child_a, child_b

    # 关：分数保持管线原值，账本无锦标赛痕迹
    off_nodes = await _tree(run_off)
    off_root, off_a, off_b = _pick(off_nodes)
    assert (off_root.score, off_a.score, off_b.score) == (0.8, 0.25, 0.25)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_off)
        state = (run.checkpoint or {})["discovery"]
        off_artifact = json.loads((run.checkpoint or {})["artifacts"]["discovery-summary.json"])
    assert "tournament" not in state
    assert all("tournament" not in h for h in off_artifact["hypotheses"])
    assert [h["score"] for h in off_artifact["hypotheses"]] == [0.8, 0.25, 0.25]

    # 开：win-rate 混分改写节点 score 与方案排序
    on_nodes = await _tree(run_on)
    root, child_a, child_b = _pick(on_nodes)
    assert (root.score, child_a.score, child_b.score) == (0.9, 0.375, 0.125)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_on)
        state = (run.checkpoint or {})["discovery"]
        artifact = json.loads((run.checkpoint or {})["artifacts"]["discovery-summary.json"])
    t_state = state["tournament"]
    # 3 个存活假设 round-robin 3 场；fake 规则下小字典序一方全胜（都记成 a 胜，
    # 因为配对按 score 排位生成、排位高的一方恰好都是字典序小的）
    assert len(t_state["matches"]) == 3
    assert [m["winner"] for m in t_state["matches"]] == ["a", "a", "a"]
    assert all("fake-compare" in m["rationale"] for m in t_state["matches"])
    assert all(m["round"] == 1 for m in t_state["matches"])
    assert t_state["nodes"][str(root.id)] == {
        "win_rate": 1.0,
        "matches": 2,
        "pipeline_score": 0.8,
        "score": 0.9,
    }
    assert t_state["nodes"][str(child_a.id)]["win_rate"] == 0.5
    assert t_state["nodes"][str(child_b.id)]["win_rate"] == 0.0
    # 轮次账本挂了本轮对阵数
    assert state["rounds"]["1"]["tournament_matches"] == 3
    # 方案产物：排序反映混合分，参赛节点带 tournament 字段
    assert [h["score"] for h in artifact["hypotheses"]] == [0.9, 0.375, 0.125]
    assert [h["tournament"]["win_rate"] for h in artifact["hypotheses"]] == [1.0, 0.5, 0.0]
    assert all(h["tournament"]["matches"] == 2 for h in artifact["hypotheses"])
    # 记账：锦标赛 token 记在参赛节点头上——开局的每个节点都比关局同位节点花得多
    off_usage, on_usage = await _node_usage_of(run_off), await _node_usage_of(run_on)
    for on_node, off_node in ((root, off_root), (child_a, off_a), (child_b, off_b)):
        assert (
            on_usage[str(on_node.id)]["prompt_tokens"]
            > off_usage[str(off_node.id)]["prompt_tokens"]
        )

    # 披露端点：开局回对阵与终榜，关局如实回空结构
    resp = await client.get(f"/api/voyages/{run_on}/tournament", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["matches"]) == 3 and set(body["nodes"]) == {
        str(root.id),
        str(child_a.id),
        str(child_b.id),
    }
    resp = await client.get(f"/api/voyages/{run_off}/tournament", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"matches": [], "nodes": {}}


async def test_tournament_multi_round_keeps_pipeline_prior(client, queue_stub):
    """两轮扩展：第二轮锦标赛沿用参赛老将首赛留档的 pipeline 分（root 0.8、
    fake A 0.25），对阵记录跨轮累加（3 + C(5,2)=10 = 13 场）。"""
    project_id, library_id, headers = await _make_workspace(client)
    resp = await client.post(
        "/api/voyages",
        json={
            "kind": "discovery",
            "project_id": project_id,
            "goal": "可验证的 agent 工作流",
            "params": {
                "direction": "可验证的 agent 工作流",
                "library_id": str(library_id),
                "max_expansions": 2,
                "tournament": True,
            },
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    run_id = uuid.UUID(resp.json()["id"])
    await VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter()).run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"
        state = (run.checkpoint or {})["discovery"]
        nodes = (
            (
                await session.execute(
                    select(HypothesisNode)
                    .where(HypothesisNode.run_id == run_id)
                    .order_by(HypothesisNode.created_at, HypothesisNode.id)
                )
            )
            .scalars()
            .all()
        )
    t_state = state["tournament"]
    assert [m["round"] for m in t_state["matches"]] == [1] * 3 + [2] * 10
    # 第一轮的三位老将第二轮继续参赛：pipeline 分保持首赛留档，不被混合分覆盖
    root = next(n for n in nodes if n.parent_id is None)
    round1_a = next(n for n in nodes if n.parent_id == root.id and "fake A" in n.statement)
    assert t_state["nodes"][str(root.id)]["pipeline_score"] == 0.8
    assert t_state["nodes"][str(round1_a.id)]["pipeline_score"] == 0.25
    # 第二轮 5 个存活假设全员入场（1 根 + 4 子，各打 4 场）
    round2_entries = [e for e in t_state["nodes"].values() if e["matches"] == 4]
    assert len(round2_entries) == 5
    # root 字典序最小（围 < 机 < 证）依旧全胜，混合分 = 0.5×0.8 + 0.5×1.0
    assert t_state["nodes"][str(root.id)] == {
        "win_rate": 1.0,
        "matches": 4,
        "pipeline_score": 0.8,
        "score": 0.9,
    }
