"""discovery 披露报告（#655，§8.2 防失败模式，P2 D6）。

两层：
- build_disclosure 纯函数——伪造 checkpoint + 轻量节点替身直测组装逻辑
  （queries 拉平、retrieved/cited 差集、时间线与级联剪枝反查、记账汇总、
  不变量违反只记 warnings 不抛错）；
- fake 管线端到端 + 产物只读端点——run 跑完后披露产物落盘且自洽，
  GET /voyages/{id}/artifacts/{name} 的白名单与 404 口径。
"""

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.voyage.engine import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.hypothesis import HypothesisNode
from app.models.paper import PaperChunk
from app.services.discovery_disclosure import build_disclosure
from tests.conftest import (
    RecordingBus,
    add_paper,
    make_project_with_library,
    register_and_login,
)

# ---- 纯函数：伪造一次两轮探索的完整现场 ----

ROOT, CHILD_A, CHILD_B, GRANDCHILD = "n-root", "n-a", "n-b", "n-ba"
P1, P2, P3, P4 = "p1", "p2", "p3", "p4"


def _node(node_id, parent_id, status, *, score=0.5, grounding=None, novelty=None):
    return SimpleNamespace(
        id=node_id,
        parent_id=parent_id,
        statement=f"假设 {node_id}",
        status=status,
        score=score,
        grounding=grounding,
        novelty_report=novelty,
    )


def _fixture():
    """根扩出 A/B；B 被显式剪枝（有决策），B 的孩子随之级联（无决策）。

    检索留痕：generate 捞到 P1/P2（灵感另含 P3），A 的接地查询捞 P1、查新捞
    P2；实引 = A 的 grounding 引 P1 + 查新引 P2 → P3 是「检了没用」差集。
    """
    nodes = [
        _node(ROOT, None, "expanded", score=0.8),
        _node(
            CHILD_A,
            ROOT,
            "open",
            score=0.25,
            grounding=[
                {"subclaim": "s1", "stance": "support", "paper_ids": [P1]},
                {"subclaim": "s2", "stance": "speculation", "paper_ids": []},
            ],
            novelty={
                "subclaims": [{"subclaim": "s1", "verdict": "novel", "paper_ids": [P2]}]
            },
        ),
        _node(CHILD_B, ROOT, "pruned", score=0.1),
        _node(GRANDCHILD, CHILD_B, "pruned", score=None),
    ]
    checkpoint = {
        "discovery": {
            "rounds": {
                "1": {
                    "node_id": ROOT,
                    "children": [CHILD_A, CHILD_B],
                    "pruned": [],
                    "queries": [
                        {
                            "phase": "generate",
                            "node_id": ROOT,
                            "query": "方向",
                            "paper_ids": [P1, P2],
                        },
                        {
                            "phase": "ground",
                            "node_id": CHILD_A,
                            "query": "s1",
                            "paper_ids": [P1],
                        },
                        {
                            "phase": "novelty",
                            "node_id": CHILD_A,
                            "query": "已有研究 s1",
                            "paper_ids": [P2],
                        },
                    ],
                    "inspiration_paper_ids": {
                        "semantic": [P1, P2],
                        "citation_far": [P3],
                        "diverse": [],
                    },
                },
            },
            "decisions": [
                {"round": 0, "decision": "expand", "node_id": ROOT, "why": "树为空"},
                {"round": 2, "decision": "pruned", "node_id": CHILD_B, "why": "方向不通"},
                {"round": 1, "decision": "summarize", "node_id": ROOT, "why": "上限"},
            ],
            "node_usage": {
                ROOT: {"prompt_tokens": 10, "completion_tokens": 5},
                CHILD_A: {"prompt_tokens": 7, "completion_tokens": 3},
            },
            "tournament": {
                "matches": [
                    {"round": 1, "a": ROOT, "b": CHILD_A, "winner": "a", "rationale": "r"}
                ],
                "nodes": {
                    ROOT: {"win_rate": 1.0, "matches": 1, "pipeline_score": 0.8, "score": 0.9}
                },
            },
        }
    }
    return checkpoint, nodes


def test_build_disclosure_full_fixture():
    checkpoint, nodes = _fixture()
    d = build_disclosure(checkpoint, nodes)

    # queries：按轮拉平，轮号/阶段/归属/查询/结果全录
    assert [(q["round"], q["phase"], q["node_id"]) for q in d["queries"]] == [
        (1, "generate", ROOT),
        (1, "ground", CHILD_A),
        (1, "novelty", CHILD_A),
    ]
    assert d["queries"][0]["paper_ids"] == [P1, P2]

    # papers：retrieved = 查询命中 ∪ 灵感论文；cited = 接地立场引用 + 查新引用
    assert d["papers"]["retrieved"] == [P1, P2, P3]
    assert d["papers"]["cited"] == [P1, P2]
    assert d["papers"]["retrieved_not_cited"] == [P3]
    assert d["papers"]["cited_not_retrieved"] == []

    # branches：根第 0 轮出生、第 1 轮被扩展；B 有直接剪枝原因；
    # B 的孩子无决策 → 反查到级联来源 B
    by_id = {b["node_id"]: b for b in d["branches"]}
    assert by_id[ROOT]["timeline"] == [
        {"event": "created", "round": 0},
        {"event": "expanded", "round": 1},
    ]
    assert by_id[CHILD_A]["timeline"] == [{"event": "created", "round": 1}]
    assert by_id[CHILD_B]["timeline"][-1] == {
        "event": "pruned",
        "round": 2,
        "reason": "方向不通",
    }
    grandchild_pruned = by_id[GRANDCHILD]["timeline"][-1]
    assert grandchild_pruned["reason"] == "随父分支级联剪枝"
    assert grandchild_pruned["cascade_from"] == CHILD_B
    # 级联节点不在任何轮次的 children 名单里（prune 动作建不出它）→ 出生轮如实 None
    assert by_id[GRANDCHILD]["timeline"][0] == {"event": "created", "round": None}

    # tournament / accounting 原样归档 + 总量
    assert d["tournament"]["matches"][0]["winner"] == "a"
    assert d["accounting"]["total"] == {"prompt_tokens": 17, "completion_tokens": 8}

    # 不变量全过、无警告
    assert d["invariants"] == {
        "cited_subset_of_retrieved": True,
        "pruned_have_reasons": True,
    }
    assert d["warnings"] == []


def test_build_disclosure_records_violations_as_warnings():
    """三条伤口都如实记 warnings 而不抛错：引用越过检索集、被剪查无原因、
    断点补账/旧版本轮次没有检索留痕。"""
    checkpoint, nodes = _fixture()
    state = checkpoint["discovery"]
    # ① A 引了一篇从未检索到的论文
    nodes[1].grounding[0]["paper_ids"] = [P4]
    # ② B 的剪枝决策丢了（连级联来源也查不到）
    state["decisions"] = [d for d in state["decisions"] if d.get("decision") != "pruned"]
    # ③ 第 2 轮是旧格式：真扩展过但没有 queries/inspiration 留痕
    state["rounds"]["2"] = {"node_id": CHILD_A, "children": [], "pruned": []}
    # ④ 断点补账轮：node_id 为 None + 标志位，属合法无留痕，不该报警
    state["rounds"]["3"] = {"node_id": None, "replayed_from_tree": True}

    d = build_disclosure(checkpoint, nodes)
    codes = [w["code"] for w in d["warnings"]]
    assert codes.count("round_without_trace") == 1
    assert {"code": "cited_not_retrieved", "paper_ids": [P4]} in d["warnings"]
    assert {"code": "pruned_without_reason", "node_id": CHILD_B} in d["warnings"]
    assert {"code": "pruned_without_reason", "node_id": GRANDCHILD} in d["warnings"]
    assert d["invariants"] == {
        "cited_subset_of_retrieved": False,
        "pruned_have_reasons": False,
    }
    assert P4 in d["papers"]["cited_not_retrieved"]
    # 时间线如实开天窗，而不是编一个原因
    by_id = {b["node_id"]: b for b in d["branches"]}
    assert by_id[CHILD_B]["timeline"][-1] == {"event": "pruned", "round": None, "reason": None}


def test_build_disclosure_tolerates_empty_run():
    """空 checkpoint / 空树：出一份空而完整的报告，不炸。"""
    d = build_disclosure({}, [])
    assert d["queries"] == [] and d["branches"] == []
    assert d["papers"]["retrieved"] == [] and d["papers"]["cited"] == []
    assert d["invariants"] == {
        "cited_subset_of_retrieved": True,
        "pruned_have_reasons": True,
    }
    assert d["accounting"]["total"] == {"prompt_tokens": 0, "completion_tokens": 0}


# ---- fake 管线端到端 + 产物端点 ----

_CORPUS = (
    "agent planning verification corpus for discovery. "
    "本库覆盖：披露测试方向 的相关论述与实验证据。" + "库内语料的细节论述。" * 30
)


async def _run_discovery(client, queue_stub) -> tuple[str, dict]:
    """API 建 run（走正规入口，可见性/参数定形与生产一致）→ 引擎同步跑完。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="disclosure-lib", statement="披露测试库"
    )
    async with get_sessionmaker()() as session:
        for i in range(2):
            paper = await add_paper(
                session,
                project_id=project_id,
                title=f"Disclosure Paper {i}",
                abstract=f"abstract {i}",
                year=2024 + i,
                venue="TestConf",
                relevance_score=0.9,
                status="compiled",
            )
            session.add(
                PaperChunk(
                    paper_id=paper.id, seq=0, text=f"{_CORPUS}（第 {i} 篇）", source="fulltext"
                )
            )
        await session.commit()
    resp = await client.post(
        "/api/voyages",
        json={
            "kind": "discovery",
            "project_id": project_id,
            "goal": "披露测试方向",
            "params": {
                "direction": "披露测试方向",
                "library_id": str(library_id),
                "max_expansions": 1,
            },
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    await VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter()).run(uuid.UUID(run_id))
    return run_id, headers


@pytest.mark.asyncio
async def test_disclosure_artifact_written_and_served(client, queue_stub):
    run_id, headers = await _run_discovery(client, queue_stub)

    resp = await client.get(
        f"/api/voyages/{run_id}/artifacts/discovery-disclosure.json", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "discovery-disclosure.json"
    d = body["content"]

    # 真 run 自洽：不变量全过、无警告
    assert d["invariants"] == {
        "cited_subset_of_retrieved": True,
        "pruned_have_reasons": True,
    }
    assert d["warnings"] == []
    # 检索留痕齐全：generate 1 条 + 每子假设接地 2 条/查新 1 条（fake 确定性形状）
    phases = [q["phase"] for q in d["queries"]]
    assert phases == [
        "generate", "ground", "ground", "novelty", "ground", "ground", "novelty",
    ]
    assert all(q["round"] == 1 and q["node_id"] for q in d["queries"])
    # 实引 ⊆ 检索全集，且与树上节点的引用一致
    assert set(d["papers"]["cited"]) <= set(d["papers"]["retrieved"])
    async with get_sessionmaker()() as session:
        nodes = (
            (
                await session.execute(
                    select(HypothesisNode).where(HypothesisNode.run_id == uuid.UUID(run_id))
                )
            )
            .scalars()
            .all()
        )
    cited_from_tree = {
        pid
        for n in nodes
        for g in (n.grounding or [])
        if g.get("stance") in ("support", "refute")
        for pid in g["paper_ids"]
    }
    assert cited_from_tree <= set(d["papers"]["cited"])
    # branches 覆盖全树，根有 created+expanded 时间线
    assert {b["node_id"] for b in d["branches"]} == {str(n.id) for n in nodes}
    root_branch = next(b for b in d["branches"] if b["parent_id"] is None)
    assert [e["event"] for e in root_branch["timeline"]] == ["created", "expanded"]
    # 记账总量 > 0
    assert d["accounting"]["total"]["prompt_tokens"] > 0

    # 与 checkpoint 里的产物逐字一致（端点只是解析转发，不加工）
    async with get_sessionmaker()() as session:
        from app.models.voyage import VoyageRun

        run = await session.get(VoyageRun, uuid.UUID(run_id))
        stored = json.loads(run.checkpoint["artifacts"]["discovery-disclosure.json"])
    assert d == stored

    # 研究方案产物同端点可读（D5 标注的缺口：此前 summary 无只读端点）
    resp = await client.get(
        f"/api/voyages/{run_id}/artifacts/discovery-summary.json", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["content"]["direction"] == "披露测试方向"


@pytest.mark.asyncio
async def test_artifact_endpoint_whitelist_and_visibility(client, queue_stub):
    run_id, headers = await _run_discovery(client, queue_stub)

    # 白名单外一律 404（哪怕 checkpoint 里真有别的键，也不泄露存在性）
    for name in ("params", "checkpoint.json", "discovery-summary", "..%2Fparams"):
        resp = await client.get(f"/api/voyages/{run_id}/artifacts/{name}", headers=headers)
        assert resp.status_code == 404, name
        assert resp.json()["detail"] in ("ARTIFACT_NOT_FOUND", "Not Found")

    # 别人的 run：404，与任务详情同口径（不泄露存在性）
    other = await register_and_login(client, email="other@example.com")
    resp = await client.get(
        f"/api/voyages/{run_id}/artifacts/discovery-disclosure.json",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404

    # run 不存在：404
    resp = await client.get(
        f"/api/voyages/{uuid.uuid4()}/artifacts/discovery-disclosure.json", headers=headers
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_artifact_endpoint_404_before_summarize(client, queue_stub):
    """还没跑到汇总的 run：产物尚未产出，404（前端据此降级为推断视图）。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="empty-lib", statement="s"
    )
    resp = await client.post(
        "/api/voyages",
        json={
            "kind": "discovery",
            "project_id": project_id,
            "goal": "g",
            "params": {"direction": "g", "library_id": str(library_id)},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    resp = await client.get(
        f"/api/voyages/{run_id}/artifacts/discovery-disclosure.json", headers=headers
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "ARTIFACT_NOT_FOUND"
