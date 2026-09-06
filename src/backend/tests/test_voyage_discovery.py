"""discovery 任务（#642 D2 + #648 D3）：树搜索规划 + 四段假设管线端到端。

- 计划模板只有播种一步（树才是真源），后续每轮由 plan_signal 经确定性分支表展开；
- 创建入口的参数定形（direction/library_id 必填 / max_expansions 默认 3 /
  tournament 先存不实现）+ 库可见性校验；
- fake provider 下端到端：每轮扩展走 generate → ground → novelty → feasibility →
  score 管线，子节点带三类证据数据落库（fake 确定性中间态 score=0.25）；
- 低分（score < 0.15）子候选自动剪枝（级联留痕语义与 hypothesis.prune 一致）；
- 汇总产物升级为研究方案（存活假设按 score 降序 + 被剪分支附录）；
- max_expansions 截止；
- 断点恢复两种杀点：扩展前被杀（管线全部发生在动树之前，重启即干净重跑、
  best_open_node 确定性重选同一节点）、子节点落库后 checkpoint 回写前被杀
  （树上 expanded 计数补账，不重复扩）。
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
from app.models.paper import PaperChunk
from app.models.voyage import VoyageRun, VoyageStep, mode_for_kind
from tests.conftest import (
    RecordingBus,
    add_paper,
    make_project_with_library,
    register_and_login,
)

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

# 库内语料：覆盖各测试用到的方向短语（fake 管线的子命题会回显方向文本，关键词
# 检索按整段中文 token 匹配，所以语料必须**逐字包含**这些短语才能命中接地检索）
_CORPUS_HIT = (
    "agent planning verification corpus for discovery. "
    "本库覆盖：可验证的 agent 工作流、同一方向、截止测试方向、恢复测试方向二、"
    "剪枝测试方向 的相关论述与实验证据。" + "库内语料的细节论述。" * 30
)
# 与任何方向都不相干的语料（低分自动剪枝路径专用：接地检索必然空手而归）
_CORPUS_MISS = "alpine botanical taxonomy notes. " + "高山植物志的形态描述。" * 30


async def _make_workspace(client, *, corpus: str = _CORPUS_HIT) -> tuple[str, uuid.UUID, dict]:
    """建课题 + 关联库 + 两篇带片段的成员论文，返回 (project_id, library_id, headers)。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="discovery-lib", statement="discovery 管线测试库"
    )
    async with get_sessionmaker()() as session:
        for i in range(2):
            paper = await add_paper(
                session,
                project_id=project_id,
                title=f"Discovery Corpus Paper {i}",
                abstract=f"corpus abstract {i}",
                year=2024 + i,
                venue="TestConf",
                relevance_score=0.9,
                status="compiled",
            )
            session.add(
                PaperChunk(
                    paper_id=paper.id, seq=0, text=f"{corpus}（第 {i} 篇）", source="fulltext"
                )
            )
        await session.commit()
    return project_id, library_id, headers


async def _make_run(
    project_id: str, library_id: uuid.UUID, *, direction: str, max_expansions: int
) -> uuid.UUID:
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
                    "library_id": str(library_id),
                    "max_expansions": max_expansions,
                    "tournament": False,
                }
            },
            project_id=uuid.UUID(project_id),
            library_id=library_id,
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


async def _library_paper_ids(library_id: uuid.UUID) -> set[str]:
    from app.models.library_direction import LibraryPaper

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(LibraryPaper.paper_id).where(LibraryPaper.library_id == library_id)
            )
        ).scalars()
        return {str(pid) for pid in rows}


# ---- 创建入口（通用 POST /api/voyages） ----


async def test_discovery_create_api_normalizes_params(client, queue_stub):
    project_id, library_id, headers = await _make_workspace(client)
    resp = await client.post(
        "/api/voyages",
        json={
            "kind": "discovery",
            "project_id": project_id,
            "goal": "LLM agent 的长期记忆机制",
            "params": {
                "direction": "  LLM agent 的长期记忆机制  ",
                "library_id": str(library_id),
            },
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    assert ("run_voyage", (run_id,), {}) in queue_stub.jobs
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, uuid.UUID(run_id))
        params = (run.checkpoint or {})["params"]
        # 参数在创建时定形：方向去空白、扩展数缺省 3、tournament 先存不实现；
        # 库关联同时落列（费用记账/列表过滤按列走，params 只是输入存档）
        assert params["direction"] == "LLM agent 的长期记忆机制"
        assert params["library_id"] == str(library_id)
        assert params["max_expansions"] == 3
        assert params["tournament"] is False
        assert run.library_id == library_id

    # direction/library_id 缺失、max_expansions 非法 → 422（引擎端不再兜底坏参数）
    for bad_params in (
        {},
        {"direction": "x"},  # #648 起 library_id 必填
        {"direction": "x", "library_id": "not-a-uuid"},
        {"direction": "x", "library_id": str(library_id), "max_expansions": -1},
        {"direction": "x", "library_id": str(library_id), "max_expansions": "many"},
    ):
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

    # 库不存在（或不可见）→ 404，与库只读端点同口径
    resp = await client.post(
        "/api/voyages",
        json={
            "kind": "discovery",
            "project_id": project_id,
            "goal": "g",
            "params": {"direction": "x", "library_id": str(uuid.uuid4())},
        },
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "LIBRARY_NOT_FOUND"


# ---- fake 管线 run 端到端 ----


async def test_discovery_pipeline_run(client, queue_stub):
    """max_expansions=1 的最小闭环：播种 → 一轮管线扩展 → 汇总 → done。"""
    project_id, library_id, _headers = await _make_workspace(client)
    run_id = await _make_run(
        project_id, library_id, direction="可验证的 agent 工作流", max_expansions=1
    )
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

        # 树结构：根（expanded，方向回显）+ 两个 open 子假设（fake 管线中间态）
        nodes = await _tree(run_id)
        assert len(nodes) == 3
        root, child_a, child_b = nodes
        assert root.parent_id is None and root.status == "expanded"
        assert "可验证的 agent 工作流" in root.statement
        assert root.score == 0.8
        assert {child_a.parent_id, child_b.parent_id} == {root.id}
        assert child_a.status == "open" and child_b.status == "open"
        assert "fake A" in child_a.statement and "fake B" in child_b.statement

        member_ids = await _library_paper_ids(library_id)
        for child in (child_a, child_b):
            # 接地：2 条子命题——第一条 support（引用真实库内论文 + 回填片段），
            # 第二条 speculation（fake 的确定性中间态）
            grounding = child.grounding
            assert [g["stance"] for g in grounding] == ["support", "speculation"]
            assert grounding[0]["paper_ids"] and set(grounding[0]["paper_ids"]) <= member_ids
            assert grounding[0]["snippets"]
            assert grounding[1]["paper_ids"] == [] and grounding[1]["snippets"] == []
            # 查新：support 子命题判 novel（引证据），speculation 不送 judge、如实 uncertain
            report = child.novelty_report["subclaims"]
            assert [r["verdict"] for r in report] == ["novel", "uncertain"]
            assert [r["judged"] for r in report] == [True, False]
            assert set(report[0]["paper_ids"]) <= member_ids
            # 可行性：确定性信号 + fake 风险论证
            signals = child.feasibility["signals"]
            assert signals["grounded_paper_count"] == 1
            assert signals["related_chunk_hits"] >= 1
            assert signals["venues"] == {"TestConf": 1}
            assert "fake 可行性" in child.feasibility["risk_note"]
            # score = novel 比例 (1/2) × support 覆盖率 (1/2)
            assert child.score == 0.25

        # 汇总产物 = 研究方案：存活假设按 score 降序、被剪附录为空、全节点快照
        artifact = json.loads((run.checkpoint or {})["artifacts"]["discovery-summary.json"])
        assert artifact["stats"] == {"expanded": 1, "open": 2}
        assert artifact["direction"] == "可验证的 agent 工作流"
        assert [h["score"] for h in artifact["hypotheses"]] == [0.8, 0.25, 0.25]
        assert artifact["hypotheses"][0]["id"] == str(root.id)
        # 证据卡数据随方案携带（前端/导出直接消费）
        assert artifact["hypotheses"][1]["grounding"][0]["stance"] == "support"
        assert artifact["hypotheses"][1]["novelty_report"]["subclaims"]
        assert artifact["hypotheses"][1]["feasibility"]["signals"]
        assert artifact["pruned_appendix"] == []
        assert {n["id"] for n in artifact["nodes"]} == {str(n.id) for n in nodes}
        assert artifact["summary"].startswith("（fake discovery 总结）")

        # 每轮决策留痕：动作、选中节点、为什么
        state = (run.checkpoint or {})["discovery"]
        decisions = state["decisions"]
        assert [d["decision"] for d in decisions] == ["expand", "summarize", "summarized"]
        assert all(d["why"] for d in decisions)
        assert decisions[1]["node_id"] == str(root.id)  # 第 1 轮扩的就是根

        # 每节点记账：seed+generate 记到根；每个子节点记它自己的接地/查新/可行性
        usage = state["node_usage"][str(root.id)]
        assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0
        for child in (child_a, child_b):
            child_usage = state["node_usage"][str(child.id)]
            assert child_usage["prompt_tokens"] > 0 and child_usage["completion_tokens"] > 0
        # 产物里的节点快照带着账本
        root_snapshot = next(n for n in artifact["nodes"] if n["id"] == str(root.id))
        assert root_snapshot["usage"] == usage


async def test_discovery_run_is_replayable(client, queue_stub):
    """确定性：同参数两次管线 run 长出同形状的树（检索/取材/评分全无随机源）。"""
    project_id, library_id, _headers = await _make_workspace(client)
    shapes = []
    for _ in range(2):
        run_id = await _make_run(project_id, library_id, direction="同一方向", max_expansions=1)
        await _engine().run(run_id)
        nodes = await _tree(run_id)
        shapes.append(
            [
                (n.statement, n.status, n.score, n.parent_id is None, json.dumps(n.grounding))
                for n in nodes
            ]
        )
    assert shapes[0] == shapes[1]


async def test_discovery_low_score_children_auto_pruned(client, queue_stub):
    """库里没有任何相关语料：接地检索空手 → 全 speculation → score=0 < 0.15 →
    两个子候选当场剪枝（留痕不删）→ 无 open 节点 → 直接转汇总。"""
    project_id, library_id, _headers = await _make_workspace(client, corpus=_CORPUS_MISS)
    run_id = await _make_run(project_id, library_id, direction="东岸洋流建模", max_expansions=3)
    await _engine().run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"
        state = (run.checkpoint or {})["discovery"]
    nodes = await _tree(run_id)
    assert len(nodes) == 3
    root = next(n for n in nodes if n.parent_id is None)
    children = [n for n in nodes if n.parent_id is not None]
    assert root.status == "expanded"
    # 无接地证据的候选：全 speculation → score 0 → 自动剪枝
    assert all(n.status == "pruned" and n.score == 0.0 for n in children)
    assert all(g["stance"] == "speculation" for n in children for g in n.grounding)
    # 剪枝决策留痕（含阈值依据），账本记了本轮的被剪名单
    pruned_ids = {str(n.id) for n in children}
    pruned_decisions = [d for d in state["decisions"] if d["decision"] == "pruned"]
    assert {d["node_id"] for d in pruned_decisions} == pruned_ids
    assert all("自动剪枝" in d["why"] for d in pruned_decisions)
    assert set(state["rounds"]["1"]["pruned"]) == pruned_ids
    # 汇总产物：方案主体只剩根，附录如实收录被剪分支及原因
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        artifact = json.loads((run.checkpoint or {})["artifacts"]["discovery-summary.json"])
    assert [h["id"] for h in artifact["hypotheses"]] == [str(root.id)]
    assert {p["id"] for p in artifact["pruned_appendix"]} == pruned_ids
    assert all("自动剪枝" in p["reason"] for p in artifact["pruned_appendix"])


async def test_discovery_max_expansions_cutoff(client, queue_stub):
    """扩展数达到上限即转汇总：2 轮 → 1 根 + 2×2 子 = 5 节点、2 个 expanded。"""
    project_id, library_id, _headers = await _make_workspace(client)
    run_id = await _make_run(project_id, library_id, direction="截止测试方向", max_expansions=2)
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
    # 第 2 轮扩的是第 1 轮的先建子候选（同分 0.25，best_open_node 同分取先建）
    round2_parent = next(n for n in nodes if n.status == "expanded" and n.parent_id is not None)
    assert "fake A" in round2_parent.statement


# ---- 断点恢复（kill 中途重启，从 checkpoint + best_open_node 确定性重建） ----


class _Killed(SystemExit):
    """模拟 worker 被杀：BaseException，穿透 Helm 的 except Exception 直接掀翻驱动。"""


async def _run_expecting_kill(run_id: uuid.UUID) -> None:
    with pytest.raises(_Killed):
        await _engine().run(run_id)


async def test_discovery_resume_kill_before_mutation(client, queue_stub):
    """杀点①：第 2 轮扩展动手前被杀。管线的 LLM/检索全部发生在动树之前，重启后
    步骤复位重跑；best_open_node 对同一棵树是确定性的——重选到同一个节点，
    最终树与不被杀完全一致。"""
    project_id, library_id, _headers = await _make_workspace(client)
    run_id = await _make_run(project_id, library_id, direction="恢复测试方向", max_expansions=2)

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
        # 两轮都正常入账；第 2 轮选中的目标 = 确定性 best_open_node（先建子候选）
        assert set(state["rounds"]) == {"1", "2"}
        assert state["rounds"]["2"]["node_id"] is not None
    nodes = await _tree(run_id)
    assert len(nodes) == 5
    round2_parent = next(n for n in nodes if str(n.id) == state["rounds"]["2"]["node_id"])
    assert "fake A" in round2_parent.statement and round2_parent.status == "expanded"


async def test_discovery_resume_kill_after_mutation(client, queue_stub):
    """杀点②：第 2 轮子节点已落库、checkpoint 还没回写就被杀。重启后账本比树少
    一轮，靠树上的 expanded 计数补账、绝不重复扩——总节点数不变。"""
    project_id, library_id, _headers = await _make_workspace(client)
    run_id = await _make_run(project_id, library_id, direction="恢复测试方向二", max_expansions=2)

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
    """hypothesis.prune：写 score + 级联剪枝（留痕不删），fake 管线跑完后手动调用。"""
    from app.agents.voyage.actions import ActionContext

    project_id, library_id, _headers = await _make_workspace(client)
    run_id = await _make_run(project_id, library_id, direction="剪枝测试方向", max_expansions=2)
    await _engine().run(run_id)
    nodes = await _tree(run_id)
    # 第 1 轮的先建子候选已被第 2 轮扩展：剪它应级联剪掉它的两个孩子
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
