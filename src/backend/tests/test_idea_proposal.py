"""Idea 深度生成（idea_proposal）全流程测试：fake LLM 全离线，直接驱动 VoyageEngine。

覆盖：目标构建（工具循环 + 机械验收）→ idea_goal 闸门（审批意见并入）→ 方案深耕
→ 新颖性核查 → 评审修订 → Research Proposal 入库；duplicate → idea_pivot 闸门 →
调整方向续跑；并发 409 / 种子校验 / 权限；跳过目标确认。

外部检索一律 external_search=False（离线）。
"""

import uuid

from sqlalchemy import select

from app.agents.voyage import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.gate import Gate
from app.models.idea import Idea
from app.models.review import ReviewMessage, ReviewSession
from tests.conftest import RecordingBus, add_paper, register_and_login

STATEMENT = "自动化科研 agent 的方法研究"

KNOBS = {
    "confirm_goal": True,
    "max_tool_calls": 5,
    "external_search": False,
    "revise_rounds": 1,
}


async def _setup_project(client, *, statement=STATEMENT, email="alice@example.com"):
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects",
        json={"name": "deep-proj", "statement": statement},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


async def _seed_searchable_papers(project_id: str, statement: str, n: int = 3) -> list[str]:
    """abstract 内嵌 statement，保证 fake 目标构建的关键词检索能命中。"""
    async with get_sessionmaker()() as session:
        ids = []
        for i in range(n):
            paper = await add_paper(session,
                project_id=uuid.UUID(project_id),
                source="manual",
                title=f"Deep paper {i}",
                abstract=f"{statement} 相关研究之{i}",
                tldr=f"论文 {i} 的一句话总结",
                wiki_content=f"## TL;DR\n\n论文 {i} 的 wiki 页 [[Agent]]（fake）",
                status="compiled",
            )
            session.add(paper)
            await session.flush()
            ids.append(str(paper.id))
        await session.commit()
    return ids


def _make_engine() -> tuple[VoyageEngine, RecordingBus]:
    bus = RecordingBus()
    return VoyageEngine(event_bus=bus, llm_router=LLMRouter()), bus


async def _approve_pending_gate(client, headers, project_id: str, *, kind: str, comment: str):
    resp = await client.get(f"/api/gates?project_id={project_id}", headers=headers)
    pending = [g for g in resp.json() if g["kind"] == kind and g["status"] == "pending"]
    assert len(pending) == 1, resp.json()
    gate = pending[0]
    resp = await client.post(
        f"/api/gates/{gate['id']}/approve", json={"comment": comment}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return gate


async def test_deep_full_pipeline_with_goal_gate(client, queue_stub, bus_recorder):
    project_id, headers = await _setup_project(client)
    paper_ids = await _seed_searchable_papers(project_id, STATEMENT, n=3)

    resp = await client.post(
        f"/api/projects/{project_id}/ideas/deep",
        json={"seed": {"type": "text", "value": "面向 agent 自验证的深入研究"}, "knobs": KNOBS},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage = resp.json()
    assert voyage["kind"] == "idea_proposal"
    assert voyage["budget"]["max_tokens"] == 400_000  # 默认预算
    run_id = voyage["id"]
    assert ("run_voyage", (run_id,), {}) in queue_stub.jobs

    # idea 类 voyage 互斥：forge / 再次 deep 均 409
    for url, body in (
        (f"/api/projects/{project_id}/ideas/deep", {"seed": {"type": "text", "value": "x"}}),
        (f"/api/projects/{project_id}/forge", {}),
    ):
        resp = await client.post(url, json=body, headers=headers)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "IDEA_VOYAGE_ALREADY_RUNNING"

    # 跑到 idea_goal 闸门暂停
    engine, _bus = _make_engine()
    await engine.run(uuid.UUID(run_id))
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "paused_gate", detail
    assert detail["steps"][0]["action"] == "goal.explore"
    assert detail["steps"][0]["status"] == "passed"
    obs = detail["steps"][0]["observation"]
    assert obs["tool_calls"] >= 1 and obs["research_type"] == "method"

    # deep/state：运行中 + 待审批闸门
    resp = await client.get(f"/api/projects/{project_id}/ideas/deep/state", headers=headers)
    state = resp.json()
    assert state["running_voyage_id"] == run_id
    assert state["pending_gate_id"] is not None

    # 闸门 payload 含结构化 goal 与探索轨迹摘要
    gate = await _approve_pending_gate(
        client, headers, project_id, kind="idea_goal", comment="范围再收窄一点"
    )
    assert gate["payload"]["goal"]["research_type"] == "method"
    assert len(gate["payload"]["goal"]["grounding"]) == 3
    assert "探索轨迹" in gate["payload"]["trace_summary"]
    assert ("resume_voyage", (run_id,), {}) in queue_stub.jobs

    # 审批后续跑到完成
    engine2, _ = _make_engine()
    await engine2.resume(uuid.UUID(run_id))
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "done", detail
    actions = [s["action"] for s in detail["steps"]]
    assert actions == [
        "goal.explore",
        "goal.refine",
        "proposal.related_work",
        "proposal.design",
        "proposal.experiments",
        "proposal.novelty_check",
        "proposal.risks",
        "proposal.assemble",
        "proposal.review_revise",
    ]
    assert all(s["status"] == "passed" for s in detail["steps"])
    assert detail["steps"][1]["observation"]["refined"] is True  # 审批意见已并入
    review_obs = detail["steps"][8]["observation"]
    assert review_obs["rounds"] == 1 and review_obs["leftovers"] == 0

    # Research Proposal 入库形状
    async with get_sessionmaker()() as session:
        ideas = (
            (await session.execute(select(Idea).where(Idea.project_id == uuid.UUID(project_id))))
            .scalars()
            .all()
        )
        assert len(ideas) == 1
        idea = ideas[0]
        assert idea.depth == "proposal" and idea.status == "candidate"
        assert idea.research_type == "method"
        assert idea.goal["question"] and idea.goal["smoke_plan"]["metric"] == "accuracy"
        assert sorted(idea.parent_paper_ids) == sorted(paper_ids)
        library_evidence = [e for e in idea.evidence if e["source"] == "library"]
        assert len(library_evidence) == 3 and all(e["title"] for e in library_evidence)
        # 终评分数来自四位专职评审员（覆盖自评）
        assert idea.scores == {
            "novelty": 8.0,
            "operability": 7.0,
            "feasibility": 7.5,
            "impact": 8.5,
        }
        for section in (
            "## 研究目标",
            "## 背景与相关工作",
            "## 研究方案设计",
            "## 实验与评估计划",
            "## 预期成果与产出",
            "## 风险与备选方案",
            "## 新颖性核查",
            "## 遗留问题",
            "### 最小验证实验",
        ):
            assert section in idea.content, section
        for pid in paper_ids:  # grounding 论文全覆盖（[[paper:uuid]] 引用）
            assert f"[[paper:{pid}]]" in idea.content

        # 评审修订会话落库并关闭
        sessions = (
            (
                await session.execute(
                    select(ReviewSession).where(
                        ReviewSession.target_type == "idea_revision",
                        ReviewSession.target_id == idea.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(sessions) == 1 and sessions[0].status == "closed"
        assert sessions[0].payload["rounds"][0]["must_fix_count"] == 0
        messages = (
            (
                await session.execute(
                    select(ReviewMessage).where(ReviewMessage.session_id == sessions[0].id)
                )
            )
            .scalars()
            .all()
        )
        assert {m.author_name for m in messages} == {
            "新颖性评审员",
            "方法论评审员",
            "可行性评审员",
            "影响力评审员",
        }

    # API：列表过滤 / 详情扩展字段 / 会话列表含评审修订
    resp = await client.get(f"/api/projects/{project_id}/ideas?depth=proposal", headers=headers)
    items = resp.json()
    assert len(items) == 1 and items[0]["research_type"] == "method"
    resp = await client.get(
        f"/api/projects/{project_id}/ideas?research_type=benchmark", headers=headers
    )
    assert resp.json() == []
    idea_id = items[0]["id"]
    resp = await client.get(f"/api/ideas/{idea_id}", headers=headers)
    api_detail = resp.json()
    assert api_detail["goal"]["research_type"] == "method"
    assert api_detail["evidence"] and api_detail["seed_idea"] is None
    resp = await client.get(f"/api/ideas/{idea_id}/sessions", headers=headers)
    session_types = {s["target_type"] for s in resp.json()}
    assert {"idea_discussion", "idea_revision"} <= session_types

    # deep/state 收尾
    resp = await client.get(f"/api/projects/{project_id}/ideas/deep/state", headers=headers)
    state = resp.json()
    assert state["running_voyage_id"] is None and state["pending_gate_id"] is None
    assert state["last_run"]["status"] == "done"

    # WS 事件：idea.created 已发布
    # （queue_stub 场景下 engine 用 RecordingBus，此处校验 Activity 落痕）
    resp = await client.get(f"/api/projects/{project_id}/activities", headers=headers)
    if resp.status_code == 200:
        kinds = {a["kind"] for a in resp.json()}
        assert "idea.proposal_created" in kinds


async def test_deep_duplicate_pivot_and_leftover_mustfix(client, queue_stub, bus_recorder):
    """novelty 判 duplicate → idea_pivot 闸门 → 批准调整方向后回炉 design 续跑；
    评审 must_fix 修不完 → 轮次耗尽照常入池并写入遗留问题。"""
    statement = f"{STATEMENT} NOVELTY_DUP_TEST PROPOSAL_MUSTFIX_TEST"
    project_id, headers = await _setup_project(client, statement=statement)
    await _seed_searchable_papers(project_id, statement, n=3)

    resp = await client.post(
        f"/api/projects/{project_id}/ideas/deep",
        json={
            "seed": {"type": "text", "value": "撞车方向"},
            "knobs": {**KNOBS, "confirm_goal": False},
        },
        headers=headers,
    )
    run_id = resp.json()["id"]
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(run_id))

    # duplicate → 重规划插入 idea_pivot 闸门并暂停
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "paused_gate", detail
    gate = await _approve_pending_gate(
        client, headers, project_id, kind="idea_pivot", comment="换个更聚焦的切入角"
    )
    assert gate["payload"]["comparisons"], gate["payload"]

    engine2, _ = _make_engine()
    await engine2.resume(uuid.UUID(run_id))
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "done", detail
    actions = [s["action"] for s in detail["steps"]]
    # 方向调整后从 design 回炉（related_work 不重跑）
    assert "goal.refine" in actions and actions.count("proposal.novelty_check") == 1

    async with get_sessionmaker()() as session:
        idea = (
            (await session.execute(select(Idea).where(Idea.project_id == uuid.UUID(project_id))))
            .scalars()
            .one()
        )
        assert idea.depth == "proposal" and idea.status == "candidate"
        # 评审两轮（修订 1 次后轮次耗尽），遗留 must_fix 写入正文
        assert "## 遗留问题" in idea.content
        assert "fake 必须修复" in idea.content
        sessions = (
            (
                await session.execute(
                    select(ReviewSession).where(ReviewSession.target_type == "idea_revision")
                )
            )
            .scalars()
            .all()
        )
        assert len(sessions[0].payload["rounds"]) == 2
        assert sessions[0].payload["leftover_must_fix"]


async def test_deep_skip_goal_gate_runs_to_done(client, queue_stub):
    """confirm_goal=False：计划里没有目标确认步骤，一口气跑完。"""
    project_id, headers = await _setup_project(client)
    await _seed_searchable_papers(project_id, STATEMENT, n=3)
    resp = await client.post(
        f"/api/projects/{project_id}/ideas/deep",
        json={
            "seed": {"type": "text", "value": "直通车"},
            "knobs": {**KNOBS, "confirm_goal": False},
        },
        headers=headers,
    )
    run_id = resp.json()["id"]
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(run_id))
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "done", detail
    assert "goal.refine" not in [s["action"] for s in detail["steps"]]
    async with get_sessionmaker()() as session:
        gates = (
            (await session.execute(select(Gate).where(Gate.kind.in_(("idea_goal", "idea_pivot")))))
            .scalars()
            .all()
        )
        assert gates == []


async def test_deep_seed_validation_and_permissions(client, queue_stub):
    project_id, headers = await _setup_project(client)
    await _seed_searchable_papers(project_id, STATEMENT, n=1)

    # 引用型种子不存在 → 404 SEED_NOT_FOUND
    resp = await client.post(
        f"/api/projects/{project_id}/ideas/deep",
        json={"seed": {"type": "idea", "value": str(uuid.uuid4())}, "knobs": KNOBS},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "SEED_NOT_FOUND"

    # 草案种子合法：先造一个 sketch idea 再深化
    async with get_sessionmaker()() as session:
        sketch = Idea(
            project_id=uuid.UUID(project_id),
            title="草案想法",
            summary="草案概述",
            status="candidate",
            depth="sketch",
            evidence=[{"source": "signal", "title": "概念组合空白：A × B", "why": "零共现"}],
        )
        session.add(sketch)
        await session.commit()
        await session.refresh(sketch)
        sketch_id = str(sketch.id)
    resp = await client.post(
        f"/api/projects/{project_id}/ideas/deep",
        json={
            "seed": {"type": "idea", "value": sketch_id},
            "knobs": {**KNOBS, "confirm_goal": False},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(run_id))
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    assert resp.json()["status"] == "done"
    async with get_sessionmaker()() as session:
        proposal = (
            (await session.execute(select(Idea).where(Idea.depth == "proposal"))).scalars().one()
        )
        assert str(proposal.seed_idea_id) == sketch_id
        # 草案的信号依据被继承进 evidence
        assert any(e["source"] == "signal" for e in proposal.evidence)
    resp = await client.get(f"/api/ideas/{proposal.id}", headers=headers)
    assert resp.json()["seed_idea"] == {"id": sketch_id, "title": "草案想法"}

    # 非项目成员一律 404
    token_b = await register_and_login(client, email="outsider@example.com")
    headers_b = {"Authorization": f"Bearer {token_b}"}
    for method, url, body in (
        (
            "post",
            f"/api/projects/{project_id}/ideas/deep",
            {"seed": {"type": "text", "value": "x"}},
        ),
        ("get", f"/api/projects/{project_id}/ideas/deep/state", None),
    ):
        resp = await getattr(client, method)(
            url, **({"json": body} if body is not None else {}), headers=headers_b
        )
        assert resp.status_code == 404, (method, url, resp.status_code)


async def test_deep_needs_differentiation_reworks_design(client, queue_stub):
    """novelty 判 needs_differentiation：不开闸门，带诊断回炉 design 后通过。"""
    statement = f"{STATEMENT} NOVELTY_DIFF_TEST"
    project_id, headers = await _setup_project(client, statement=statement)
    await _seed_searchable_papers(project_id, statement, n=3)
    resp = await client.post(
        f"/api/projects/{project_id}/ideas/deep",
        json={
            "seed": {"type": "text", "value": "需要差异化的方向"},
            "knobs": {**KNOBS, "confirm_goal": False},
        },
        headers=headers,
    )
    run_id = resp.json()["id"]
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(run_id))
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "done", detail
    # 全程无人工审批（不产生任何闸门）
    async with get_sessionmaker()() as session:
        gates = (
            (await session.execute(select(Gate).where(Gate.kind.in_(("idea_goal", "idea_pivot")))))
            .scalars()
            .all()
        )
        assert gates == []
        idea = (await session.execute(select(Idea).where(Idea.depth == "proposal"))).scalars().one()
        # 回炉后的设计（fake 修订版）进入最终正文
        assert "（fake 修订设计）" in idea.content
