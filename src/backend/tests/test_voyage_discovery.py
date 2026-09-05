"""discovery 任务（#642，P2 D2）：树搜索规划 + fake 骨架 run 端到端。

- 计划模板只有播种一步（树才是真源），后续每轮由 plan_signal 经确定性分支表展开；
- 创建入口的参数定形（direction 必填 / max_expansions 默认 3 / tournament 先存不实现）；
- fake provider 下端到端：根 + 两子 + 状态、汇总产物（被剪分支留痕结构）、每节点记账；
- max_expansions 截止；
- 断点恢复两种杀点：扩展前被杀（重启按 best_open_node 确定性重选同一节点）、
  子节点落库后 checkpoint 回写前被杀（树上 expanded 计数补账，不重复扩）。
"""

import json
import uuid

import pytest
from sqlalchemy import select

from app.agents.voyage import actions as actions_registry
from app.agents.voyage.engine import VoyageEngine
from app.agents.voyage.navigator import discovery_plan, done_criteria_for_kind
from app.agents.voyage.plan_edit import discovery_signal_edits
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.hypothesis import HypothesisNode
from app.models.voyage import VoyageRun, VoyageStep, mode_for_kind
from tests.conftest import RecordingBus, register_and_login

# ---- 纯函数：计划模板 + 确定性分支表 ----


def test_discovery_plan_only_seeds():
    """初始计划只有播种一步：树搜索不预排线性清单，后续由树状态驱动。"""
    plan = discovery_plan(None)  # run 参数未用（模板不依赖 run 状态）
    assert [s["action"] for s in plan] == ["hypothesis.seed"]
    assert mode_for_kind("discovery") == "loop"
    criteria = done_criteria_for_kind("discovery")
    assert criteria == {
        "checks": [{"kind": "artifact_exists", "key": "artifacts.discovery-summary.json"}]
    }


class _RowStub:
    def __init__(self, action: str, status: str) -> None:
        self.action = action
        self.status = status


def test_discovery_signal_edits_idempotent():
    """分支表：expand/summarize 各追加一个节点；待办同类节点已存在则跳过（防重放）。"""
    rows = [_RowStub("hypothesis.seed", "passed")]
    edit = discovery_signal_edits({"decision": "expand", "next_round": 1}, rows)
    nodes = edit["edits"][0]["nodes"]
    assert [n["action"] for n in nodes] == ["hypothesis.expand"]
    assert nodes[0]["params"] == {"round": 1}

    rows.append(_RowStub("hypothesis.expand", "pending"))
    assert discovery_signal_edits({"decision": "expand", "next_round": 1}, rows) is None

    edit = discovery_signal_edits({"decision": "summarize", "reason": "上限已到"}, rows)
    nodes = edit["edits"][0]["nodes"]
    assert [n["action"] for n in nodes] == ["discovery.summarize"]
    assert nodes[0]["wrapup"] is True  # 预算耗尽也要能把树落成产物

    rows.append(_RowStub("discovery.summarize", "pending"))
    assert discovery_signal_edits({"decision": "summarize"}, rows) is None
    assert discovery_signal_edits({"decision": "unknown"}, rows) is None


# ---- 造数据助手 ----


async def _make_project(client) -> tuple[str, dict]:
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "discovery-proj"}, headers=headers)
    return resp.json()["id"], headers


async def _make_run(project_id: str, *, direction: str, max_expansions: int) -> uuid.UUID:
    """直建 run（plan=None：首次驱动由 navigator 的 discovery 模板补计划）。"""
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="discovery",
            goal=direction,
            status="planning",
            cursor=0,
            checkpoint={
                "params": {
                    "direction": direction,
                    "max_expansions": max_expansions,
                    "tournament": False,
                }
            },
            project_id=uuid.UUID(project_id),
        )
        session.add(run)
        await session.commit()
        return run.id


def _engine() -> VoyageEngine:
    return VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())


async def _tree(run_id: uuid.UUID) -> list[HypothesisNode]:
    async with get_sessionmaker()() as session:
        return list(
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


# ---- 创建入口（通用 POST /api/voyages） ----


async def test_discovery_create_api_normalizes_params(client, queue_stub):
    project_id, headers = await _make_project(client)
    resp = await client.post(
        "/api/voyages",
        json={
            "kind": "discovery",
            "project_id": project_id,
            "goal": "LLM agent 的长期记忆机制",
            "params": {"direction": "  LLM agent 的长期记忆机制  "},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    assert ("run_voyage", (run_id,), {}) in queue_stub.jobs
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, uuid.UUID(run_id))
        params = (run.checkpoint or {})["params"]
        # 参数在创建时定形：方向去空白、扩展数缺省 3、tournament 先存不实现
        assert params["direction"] == "LLM agent 的长期记忆机制"
        assert params["max_expansions"] == 3
        assert params["tournament"] is False

    # direction 缺失 / max_expansions 非法 → 422（引擎端不再兜底坏参数）
    for bad_params in ({}, {"direction": "x", "max_expansions": -1},
                       {"direction": "x", "max_expansions": "many"}):
        resp = await client.post(
            "/api/voyages",
            json={
                "kind": "discovery",
                "project_id": project_id,
                "goal": "g",
                "params": bad_params,
            },
            headers=headers,
        )
        assert resp.status_code == 422, bad_params


# ---- fake 骨架 run 端到端 ----


async def test_discovery_skeleton_run(client, queue_stub):
    """max_expansions=1 的最小闭环：播种 → 一轮扩展 → 汇总 → done。"""
    project_id, _headers = await _make_project(client)
    run_id = await _make_run(project_id, direction="可验证的 agent 工作流", max_expansions=1)
    await _engine().run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"
        assert run.mode == "loop"
        steps = (
            (
                await session.execute(
                    select(VoyageStep).where(VoyageStep.run_id == run_id).order_by(VoyageStep.seq)
                )
            )
            .scalars()
            .all()
        )
        # 计划由信号逐步长出来：seed → expand(1) → summarize，全部通过
        assert [s.action for s in steps] == [
            "hypothesis.seed",
            "hypothesis.expand",
            "discovery.summarize",
        ]
        assert all(s.status == "passed" for s in steps)

        # 树结构：根（expanded，方向回显）+ 两个 open 子假设
        nodes = await _tree(run_id)
        assert len(nodes) == 3
        root, child_a, child_b = nodes
        assert root.parent_id is None and root.status == "expanded"
        assert "可验证的 agent 工作流" in root.statement
        assert root.score == 0.8
        assert {child_a.parent_id, child_b.parent_id} == {root.id}
        assert child_a.status == "open" and child_b.status == "open"
        assert (child_a.score, child_b.score) == (0.7, 0.6)

        # 汇总产物：全部节点 + 状态 + 统计（被剪分支也会留在 nodes 里，这里为 0）
        artifact = json.loads((run.checkpoint or {})["artifacts"]["discovery-summary.json"])
        assert artifact["stats"] == {"expanded": 1, "open": 2}
        assert {n["id"] for n in artifact["nodes"]} == {str(n.id) for n in nodes}
        assert artifact["summary"].startswith("（fake discovery 总结）")

        # 每轮决策留痕：动作、选中节点、为什么
        state = (run.checkpoint or {})["discovery"]
        decisions = state["decisions"]
        assert [d["decision"] for d in decisions] == ["expand", "summarize", "summarized"]
        assert all(d["why"] for d in decisions)
        assert decisions[1]["node_id"] == str(root.id)  # 第 1 轮扩的就是根

        # 每节点记账：seed 记到根、expand 记到被扩展的根（只记不限）
        usage = state["node_usage"][str(root.id)]
        assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0
        # 产物里的节点快照带着账本
        root_snapshot = next(n for n in artifact["nodes"] if n["id"] == str(root.id))
        assert root_snapshot["usage"] == usage


async def test_discovery_run_is_replayable(client, queue_stub):
    """确定性：同参数两次骨架 run 长出同形状的树（fake provider 无随机）。"""
    project_id, _headers = await _make_project(client)
    shapes = []
    for _ in range(2):
        run_id = await _make_run(project_id, direction="同一方向", max_expansions=1)
        await _engine().run(run_id)
        nodes = await _tree(run_id)
        shapes.append([(n.statement, n.status, n.score, n.parent_id is None) for n in nodes])
    assert shapes[0] == shapes[1]


async def test_discovery_max_expansions_cutoff(client, queue_stub):
    """扩展数达到上限即转汇总：2 轮 → 1 根 + 2×2 子 = 5 节点、2 个 expanded。"""
    project_id, _headers = await _make_project(client)
    run_id = await _make_run(project_id, direction="截止测试方向", max_expansions=2)
    await _engine().run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"
        steps = (
            (
                await session.execute(
                    select(VoyageStep).where(VoyageStep.run_id == run_id).order_by(VoyageStep.seq)
                )
            )
            .scalars()
            .all()
        )
        assert [s.action for s in steps].count("hypothesis.expand") == 2
    nodes = await _tree(run_id)
    assert len(nodes) == 5
    assert sum(1 for n in nodes if n.status == "expanded") == 2
    assert sum(1 for n in nodes if n.status == "open") == 3
    # 第 2 轮扩的是第 1 轮的高分子假设 A（best_open_node：score 降序、同分取先建）
    round2_parent = next(n for n in nodes if n.status == "expanded" and n.parent_id is not None)
    assert "子假设 A" in round2_parent.statement


# ---- 断点恢复（kill 中途重启，从 checkpoint + best_open_node 确定性重建） ----


class _Killed(SystemExit):
    """模拟 worker 被杀：BaseException，穿透 Helm 的 except Exception 直接掀翻驱动。"""


async def _run_expecting_kill(run_id: uuid.UUID) -> None:
    with pytest.raises(_Killed):
        await _engine().run(run_id)


async def test_discovery_resume_kill_before_mutation(client, queue_stub):
    """杀点①：第 2 轮扩展动手前被杀。重启后步骤复位重跑，best_open_node 对同一棵
    树是确定性的——重选到同一个节点，最终树与不被杀完全一致。"""
    project_id, _headers = await _make_project(client)
    run_id = await _make_run(project_id, direction="恢复测试方向", max_expansions=2)

    orig = actions_registry._REGISTRY["hypothesis.expand"]
    fired = False

    async def killer(ctx, params):
        nonlocal fired
        if int(params.get("round") or 0) == 2 and not fired:
            fired = True
            raise _Killed(1)  # 动手之前被杀：树未动、checkpoint 未记
        return await orig(ctx, params)

    actions_registry._REGISTRY["hypothesis.expand"] = killer
    try:
        await _run_expecting_kill(run_id)
    finally:
        actions_registry._REGISTRY["hypothesis.expand"] = orig

    # 被杀时的现场：run 非终态、第 2 轮扩展停在 running
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "executing"
        running = (
            (
                await session.execute(
                    select(VoyageStep).where(
                        VoyageStep.run_id == run_id, VoyageStep.status == "running"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(running) == 1 and running[0].action == "hypothesis.expand"
    assert len(await _tree(run_id)) == 3  # 第 2 轮尚未动树

    await _engine().resume(run_id)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"
        state = (run.checkpoint or {})["discovery"]
        # 两轮都正常入账；第 2 轮选中的目标 = 确定性 best_open_node（高分子假设 A）
        assert set(state["rounds"]) == {"1", "2"}
        assert state["rounds"]["2"]["node_id"] is not None
    nodes = await _tree(run_id)
    assert len(nodes) == 5
    round2_parent = next(n for n in nodes if str(n.id) == state["rounds"]["2"]["node_id"])
    assert "子假设 A" in round2_parent.statement and round2_parent.status == "expanded"


async def test_discovery_resume_kill_after_mutation(client, queue_stub):
    """杀点②：第 2 轮子节点已落库、checkpoint 还没回写就被杀。重启后账本比树少
    一轮，靠树上的 expanded 计数补账、绝不重复扩——总节点数不变。"""
    project_id, _headers = await _make_project(client)
    run_id = await _make_run(project_id, direction="恢复测试方向二", max_expansions=2)

    orig = actions_registry._REGISTRY["hypothesis.expand"]
    fired = False

    async def killer(ctx, params):
        nonlocal fired
        result = await orig(ctx, params)  # 树已提交、ctx.checkpoint 只在内存里
        if int(params.get("round") or 0) == 2 and not fired:
            fired = True
            raise _Killed(1)  # engine 还没来得及把 checkpoint 写回 run
        return result

    actions_registry._REGISTRY["hypothesis.expand"] = killer
    try:
        await _run_expecting_kill(run_id)
    finally:
        actions_registry._REGISTRY["hypothesis.expand"] = orig

    assert len(await _tree(run_id)) == 5  # 第 2 轮的两个子节点已落库

    await _engine().resume(run_id)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"
        state = (run.checkpoint or {})["discovery"]
        # 第 2 轮由树上 expanded 计数补账（不知道当时选了谁，如实记 None）
        assert state["rounds"]["2"] == {"node_id": None, "replayed_from_tree": True}
        assert any("补记" in d["why"] for d in state["decisions"])
    nodes = await _tree(run_id)
    assert len(nodes) == 5  # 没有重复扩展
    assert sum(1 for n in nodes if n.status == "expanded") == 2


async def test_discovery_prune_action_cascades(client, queue_stub):
    """hypothesis.prune：写 score + 级联剪枝（留痕不删），fake 骨架跑完后手动调用。"""
    from app.agents.voyage.actions import ActionContext

    project_id, _headers = await _make_project(client)
    run_id = await _make_run(project_id, direction="剪枝测试方向", max_expansions=2)
    await _engine().run(run_id)
    nodes = await _tree(run_id)
    # 第 1 轮的子假设 A 已被第 2 轮扩展：剪它应级联剪掉它的两个孩子
    target = next(n for n in nodes if n.status == "expanded" and n.parent_id is not None)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint or {}))
        prune = actions_registry._REGISTRY["hypothesis.prune"]
        obs = await prune(ctx, {"node_id": str(target.id), "score": 0.1, "reason": "方向不通"})
        assert obs["pruned"] == str(target.id)

    nodes = await _tree(run_id)
    pruned = {str(n.id) for n in nodes if n.status == "pruned"}
    children = {str(n.id) for n in nodes if n.parent_id == target.id}
    assert str(target.id) in pruned and children <= pruned  # 级联整个子树
    assert next(n for n in nodes if n.id == target.id).score == 0.1
    assert len(nodes) == 5  # 留痕不删
