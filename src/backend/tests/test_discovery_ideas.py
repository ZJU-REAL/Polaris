"""假设探索把留下来的假设写成想法（discovery.write_ideas）。

- 选哪些假设：优先叶子、被剪的不写、根不写、凑不够三个时用父假设补；
- 字段映射：假设陈述原样做概述，证据带立场和原文片段，来路记在 origin；
- 不夸大：写作调用只拿到有依据的证据，没有依据的子命题在正文末尾逐字单列；
- 新颖性只对照文献库：理由里固定带一句、origin.novelty_scope = "library"；
- 与课题里已有的想法去重；重放这一步不重复写；
- 库里可检索的论文少于 5 篇时不建任务（409 LIBRARY_TOO_SMALL）。
"""

import json
import uuid
from types import SimpleNamespace

from sqlalchemy import select

from app.agents.voyage import actions as actions_registry
from app.agents.voyage import actions_discovery
from app.agents.voyage.actions_discovery import select_idea_nodes
from app.core.db import get_sessionmaker
from app.core.llm.errors import LLMSchemaError
from app.core.llm.fake import FakeProvider
from app.models.idea import Idea
from app.models.paper import PaperChunk
from app.models.voyage import VoyageRun, VoyageStep
from app.services import hypothesis_pipeline as pipeline
from app.services import voyages as voyages_service
from tests.conftest import add_paper, make_project_with_library, register_and_login
from tests.test_voyage_discovery import _CORPUS_HIT, _engine, _make_run, _make_workspace, _tree
from tests.vector_helpers import index_chunks

# ---- 选哪些假设（纯函数） ----


def _node(seq, *, parent=None, status="open", score=0.2, support=True, kind="hypothesis"):
    stance = "support" if support else "speculation"
    return SimpleNamespace(
        id=f"n{seq}",
        seq=seq,
        parent_id=parent,
        kind=kind,
        status=status,
        score=score,
        grounding=[{"subclaim": "s", "stance": stance, "paper_ids": ["p"] if support else []}],
    )


def test_selection_prefers_leaves_and_skips_root_pruned_and_unsupported():
    root = _node(1, status="expanded", score=0.8)
    a = _node(2, parent="n1", status="expanded", score=0.3)  # 被细化过
    b = _node(3, parent="n1", score=0.25)
    pruned = _node(4, parent="n1", status="pruned", score=0.05)
    unsupported = _node(5, parent="n1", score=0.4, support=False)
    a1 = _node(6, parent="n2", score=0.2)
    a2 = _node(7, parent="n2", score=0.22)
    experiment = _node(8, parent="n2", kind="experiment", score=0.9)
    nodes = [root, a, b, pruned, unsupported, a1, a2, experiment]
    # a 有合格的子假设 → 写更具体的 a1 / a2，不写 a 自己；按分数从高到低
    assert [n.id for n in select_idea_nodes(nodes)] == ["n3", "n7", "n6"]


def test_selection_emits_a_parent_when_none_of_its_children_qualifies():
    root = _node(1, status="expanded", score=0.8)
    a = _node(2, parent="n1", status="expanded", score=0.3)
    children = [
        _node(3, parent="n2", status="pruned", score=0.0),
        _node(4, parent="n2", support=False),
    ]
    assert [n.id for n in select_idea_nodes([root, a, *children])] == ["n2"]


def test_selection_tops_up_with_refined_parents_when_the_tree_is_narrow():
    """每轮只留下一个子假设的窄树：只写叶子只有一个想法，用父假设补到三个。"""
    root = _node(1, status="expanded", score=0.8)
    a = _node(2, parent="n1", status="expanded", score=0.3)
    b = _node(3, parent="n2", status="expanded", score=0.25)
    c = _node(4, parent="n3", score=0.2)
    assert [n.id for n in select_idea_nodes([root, a, b, c])] == ["n4", "n2", "n3"]


def test_selection_caps_at_the_limit_and_breaks_ties_by_creation_order():
    root = _node(1, status="expanded", score=0.8)
    leaves = [_node(i, parent="n1", score=0.25) for i in range(2, 14)]
    chosen = select_idea_nodes([root, *leaves])
    assert [n.id for n in chosen] == [f"n{i}" for i in range(2, 10)]  # 8 个，先建的在前


def test_prune_threshold_keeps_a_supported_hypothesis_with_one_novel_subclaim():
    """真实模型下一个像样的假设：六条子命题四条有支持、一条新颖 = 0.111，不该被剪；
    一条新颖都没有的照旧在线下。"""
    grounding = [{"stance": "support"}] * 4 + [{"stance": "speculation"}] * 2
    one_novel = {"subclaims": [{"verdict": "novel"}] + [{"verdict": "uncertain"}] * 5}
    none_novel = {"subclaims": [{"verdict": "uncertain"}] * 6}
    assert pipeline.score_hypothesis(grounding, one_novel) >= pipeline.MIN_VIABLE_SCORE
    assert pipeline.score_hypothesis(grounding, none_novel) < pipeline.MIN_VIABLE_SCORE


# ---- 端到端：探索跑完，想法在课题里 ----


async def _ideas(project_id: str) -> list[Idea]:
    async with get_sessionmaker()() as session:
        return list(
            (
                await session.execute(
                    select(Idea)
                    .where(Idea.project_id == uuid.UUID(project_id))
                    .order_by(Idea.created_at, Idea.id)
                )
            )
            .scalars()
            .all()
        )


async def test_discovery_writes_surviving_hypotheses_as_ideas(client, queue_stub, monkeypatch):
    project_id, library_id, headers = await _make_workspace(client)
    payloads: list[dict] = []
    original = FakeProvider._respond_hyp_writeup

    def _spy(last_user: str) -> str:
        payloads.append(json.loads(last_user))
        return original(last_user)

    monkeypatch.setattr(FakeProvider, "_respond_hyp_writeup", staticmethod(_spy))
    run_id = await _make_run(
        project_id, library_id, direction="可验证的 agent 工作流", max_expansions=1
    )
    await _engine().run(run_id)

    nodes = await _tree(run_id)
    root, child_a, child_b = nodes
    ideas = await _ideas(project_id)
    assert len(ideas) == 2  # 两个叶子；根不写
    by_node = {idea.origin["node_id"]: idea for idea in ideas}
    assert set(by_node) == {str(child_a.id), str(child_b.id)}

    idea = by_node[str(child_a.id)]
    assert idea.summary == child_a.statement  # 假设陈述原样做概述
    assert idea.title.startswith("想法（fake）：") and len(idea.title) <= 80
    assert idea.status == "candidate" and idea.depth == "sketch"
    assert idea.source_voyage_id == run_id
    assert idea.origin == {
        "kind": "discovery",
        "node_id": str(child_a.id),
        "pipeline_score": 0.25,
        "support_ratio": 0.5,
        "novel_ratio": 0.5,
        "novelty_scope": "library",
        "library_id": str(library_id),
        "library_paper_count": 2,
        "unsupported_subclaims": [child_a.grounding[1]["subclaim"]],
    }
    # 证据：每条（子命题, 论文）一项，立场和原文片段带上；只有有依据的子命题进来
    supported = child_a.grounding[0]
    assert [e["paper_id"] for e in idea.evidence] == supported["paper_ids"]
    entry = idea.evidence[0]
    assert entry["stance"] == "support" and entry["source"] == "library"
    assert entry["why"] == supported["subclaim"]
    assert entry["snippet"] == supported["snippets"][0]
    assert entry["title"].startswith("Discovery Corpus Paper")
    assert entry["url"] is None
    assert [str(p) for p in idea.parent_paper_ids] == supported["paper_ids"]
    # 新颖性只对照文献库：理由里固定带这一句
    assert idea.scores == {"novelty": 6, "feasibility": 7, "operability": 6, "impact": 5}
    assert idea.score_rationale["novelty"].endswith("新颖性只对照你的文献库判断。")
    # 正文：四段 + 末尾逐字单列没有依据的子命题
    assert [line for line in idea.content.splitlines() if line.startswith("## ")] == [
        "## 动机",
        "## 方法概述",
        "## 预期实验",
        "## 风险",
        "## 尚无文献依据的部分",
    ]
    assert idea.content.endswith(f"- {child_a.grounding[1]['subclaim']}")

    # 写作调用只拿到有依据的证据；没有依据的子命题只出现在 unsupported 里
    sent = next(p for p in payloads if p["statement"] == child_a.statement)
    assert [e["subclaim"] for e in sent["evidence"]] == [supported["subclaim"]]
    assert sent["evidence"][0]["papers"][0]["snippet"] == supported["snippets"][0]
    assert sent["unsupported"] == [child_a.grounding[1]["subclaim"]]
    assert sent["language"] == "zh"
    assert sent["signals"]["library_paper_count"] == 2

    # API：想法列表和详情都带来路
    listed = (await client.get(f"/api/projects/{project_id}/ideas", headers=headers)).json()
    assert {i["source_voyage_id"] for i in listed} == {str(run_id)}
    assert all(i["origin"]["novelty_scope"] == "library" for i in listed)
    detail = (await client.get(f"/api/ideas/{idea.id}", headers=headers)).json()
    assert detail["evidence"][0]["stance"] == "support"
    assert detail["origin"]["node_id"] == str(child_a.id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        ledger = run.checkpoint["discovery"]["ideas"]
        assert ledger == {
            str(child_a.id): {"idea_id": str(by_node[str(child_a.id)].id), "duplicate": False},
            str(child_b.id): {"idea_id": str(by_node[str(child_b.id)].id), "duplicate": False},
        }
        step = (
            await session.execute(
                select(VoyageStep).where(
                    VoyageStep.run_id == run_id, VoyageStep.action == "discovery.write_ideas"
                )
            )
        ).scalar_one()
        assert step.observation["ideas"] == 2 and step.observation["duplicates"] == 0


async def test_emit_ideas_false_keeps_the_tree_only(client, queue_stub):
    project_id, library_id, _headers = await _make_workspace(client)
    run_id = await _make_run(project_id, library_id, direction="同一方向", max_expansions=1)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        checkpoint = dict(run.checkpoint)
        checkpoint["params"] = {**checkpoint["params"], "emit_ideas": False}
        run.checkpoint = checkpoint
        await session.commit()
    await _engine().run(run_id)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"
        actions = (
            (
                await session.execute(
                    select(VoyageStep.action)
                    .where(VoyageStep.run_id == run_id)
                    .order_by(VoyageStep.seq)
                )
            )
            .scalars()
            .all()
        )
    assert "discovery.write_ideas" not in actions
    assert await _ideas(project_id) == []


async def _replay_write_ideas(run_id: uuid.UUID) -> dict:
    """再跑一次写想法这一步（恢复后重放同一步的等价物）。"""
    from app.agents.voyage.actions import ActionContext
    from app.core.llm.router import LLMRouter
    from tests.conftest import RecordingBus

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        checkpoint = json.loads(json.dumps(run.checkpoint))
        ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=checkpoint, bus=RecordingBus())
        result = await actions_registry.get_action("discovery.write_ideas")(ctx, {})
        run.checkpoint = checkpoint
        await session.commit()
    return result


async def test_write_ideas_replay_does_not_write_twice(client, queue_stub, monkeypatch):
    project_id, library_id, _headers = await _make_workspace(client)
    run_id = await _make_run(project_id, library_id, direction="同一方向", max_expansions=1)
    await _engine().run(run_id)
    first = await _ideas(project_id)
    assert len(first) == 2

    calls = 0
    original = FakeProvider._respond_hyp_writeup

    def _count(last_user: str) -> str:
        nonlocal calls
        calls += 1
        return original(last_user)

    monkeypatch.setattr(FakeProvider, "_respond_hyp_writeup", staticmethod(_count))
    result = await _replay_write_ideas(run_id)
    assert result["ideas"] == 2 and sorted(result["idea_ids"]) == sorted(str(i.id) for i in first)
    assert calls == 0  # 已写过的节点不再调模型
    assert [i.id for i in await _ideas(project_id)] == [i.id for i in first]


async def test_write_ideas_skips_duplicates_of_existing_ideas(client, queue_stub):
    """第二次探索同一方向：假设与第一次写下的想法相同 → 不重复写，记下与谁重复。"""
    project_id, library_id, _headers = await _make_workspace(client)
    first_run = await _make_run(project_id, library_id, direction="同一方向", max_expansions=1)
    await _engine().run(first_run)
    first = await _ideas(project_id)
    assert len(first) == 2

    second_run = await _make_run(project_id, library_id, direction="同一方向", max_expansions=1)
    await _engine().run(second_run)
    assert [i.id for i in await _ideas(project_id)] == [i.id for i in first]
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, second_run)
        assert run.status == "done"
        ledger = run.checkpoint["discovery"]["ideas"]
    assert len(ledger) == 2
    assert all(entry["duplicate"] is True for entry in ledger.values())
    assert {entry["idea_id"] for entry in ledger.values()} == {str(i.id) for i in first}

    # 用户把这些想法扔进回收站后再探索：它们不再挡住同样的想法
    async with get_sessionmaker()() as session:
        from app.models.base import utcnow

        for old in first:
            (await session.get(Idea, old.id)).trashed_at = utcnow()
        await session.commit()
    third_run = await _make_run(project_id, library_id, direction="同一方向", max_expansions=1)
    await _engine().run(third_run)
    fresh = [i for i in await _ideas(project_id) if i.trashed_at is None]
    assert len(fresh) == 2
    assert {i.source_voyage_id for i in fresh} == {third_run}
    assert {i.summary for i in fresh} == {i.summary for i in first}


async def test_write_up_failure_still_stores_the_hypothesis_and_its_evidence(
    client, queue_stub, monkeypatch
):
    """模型给不出合格的草案：想法照样入库（标题取假设、正文待补充、没有评分），
    证据和「尚无依据」一节都在。"""
    project_id, library_id, _headers = await _make_workspace(client)

    def _broken(last_user: str) -> str:
        raise LLMSchemaError("no usable draft")

    monkeypatch.setattr(FakeProvider, "_respond_hyp_writeup", staticmethod(_broken))
    run_id = await _make_run(project_id, library_id, direction="同一方向", max_expansions=1)
    await _engine().run(run_id)
    ideas = await _ideas(project_id)
    assert len(ideas) == 2
    idea = ideas[0]
    assert idea.title == idea.summary[:80]
    assert idea.scores is None and idea.score_rationale is None
    assert "（待补充）" in idea.content and "## 尚无文献依据的部分" in idea.content
    assert idea.evidence and idea.evidence[0]["stance"] == "support"


def test_english_direction_writes_english_headings():
    content = actions_discovery._idea_content({"motivation": "m"}, ["claim one"], "en")
    assert content.startswith("## Motivation\n\nm")
    assert content.endswith("## Not yet supported by the literature\n\n- claim one")
    none = actions_discovery._idea_content({}, [], "zh")
    assert none.endswith("（每条子命题都在文献库里找到了依据。）")


def test_evidence_entries_pair_each_paper_with_its_own_snippet():
    node = SimpleNamespace(
        grounding=[
            {
                "subclaim": "c1",
                "stance": "refute",
                "paper_ids": ["p2", "p1"],
                # 检索集的顺序：p1 有两段入选，p2 一段
                "snippets": ["p1-first", "p2-only", "p1-second"],
                "snippet_paper_ids": ["p1", "p2", "p1"],
            },
            {"subclaim": "c2", "stance": "speculation", "paper_ids": [], "snippets": []},
        ]
    )
    entries = actions_discovery._evidence_entries(node, {"p1": "One", "p2": "Two"})
    assert [(e["paper_id"], e["title"], e["stance"], e["snippet"]) for e in entries] == [
        ("p2", "Two", "refute", "p2-only"),
        ("p1", "One", "refute", "p1-first"),
    ]
    assert actions_discovery._unsupported_subclaims(node) == ["c2"]


# ---- 前置条件：库里可检索的论文太少就不建任务 ----


async def _library_with_papers(client, *, count: int, indexed: int):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="size-lib", statement="前置条件测试库"
    )
    async with get_sessionmaker()() as session:
        indexed_ids = []
        for i in range(count):
            paper = await add_paper(
                session,
                project_id=project_id,
                title=f"Size Paper {i}",
                abstract=f"abstract {i}",
                year=2024,
                relevance_score=0.9,
                status="compiled",
            )
            session.add(PaperChunk(paper_id=paper.id, seq=0, text=_CORPUS_HIT, source="fulltext"))
            if i < indexed:
                indexed_ids.append(paper.id)
        # 一篇还只是候选的论文：有向量也不算库内
        candidate = await add_paper(
            session, project_id=project_id, title="Candidate", status="candidate"
        )
        session.add(PaperChunk(paper_id=candidate.id, seq=0, text=_CORPUS_HIT, source="fulltext"))
        await session.commit()
        await index_chunks(session, paper_ids=[*indexed_ids, candidate.id])
    return project_id, library_id, headers


def _discovery_body(project_id, library_id):
    return {
        "kind": "discovery",
        "project_id": project_id,
        "goal": "方向",
        "params": {"direction": "方向", "library_id": str(library_id)},
    }


async def test_discovery_needs_five_indexed_library_papers(client, queue_stub):
    project_id, library_id, headers = await _library_with_papers(client, count=6, indexed=4)
    async with get_sessionmaker()() as session:
        assert await voyages_service.indexed_library_paper_count(session, library_id) == 4
    resp = await client.post(
        "/api/voyages", json=_discovery_body(project_id, library_id), headers=headers
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "LIBRARY_TOO_SMALL"
    assert queue_stub.jobs == []
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(VoyageRun))).scalars().all() == []


async def test_discovery_starts_with_five_indexed_library_papers(client, queue_stub):
    project_id, library_id, headers = await _library_with_papers(client, count=5, indexed=5)
    resp = await client.post(
        "/api/voyages", json=_discovery_body(project_id, library_id), headers=headers
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "planning"
