"""Idea Forge 全流程测试：fake LLM 全离线，直接驱动 VoyageEngine。

覆盖：forge 全链路（读上下文→gap→生成→打分→去重→入库）、并发 409（forge/review 互斥）、
语义去重丢弃、断点恢复不重复生成、forge/state、ideas API 与权限冒烟。
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.agents.voyage import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.fake import EMBEDDING_DIM, FakeProvider
from app.core.llm.router import LLMRouter
from app.models.activity import Activity
from app.models.idea import Idea
from app.models.paper import Paper
from tests.conftest import RecordingBus, register_and_login

DEFINITION = {"statement": "自动化科研 agent 的方法研究"}

KNOBS = {"num_ideas": 2, "dedup_threshold": 0.9, "max_context_papers": 20}

# fake provider 生成想法的确定性文案（app/core/llm/fake.py::_respond_forge_ideas）
FAKE_IDEA_1_TITLE = "候选想法 1：面向空白 g1 的方法（fake）"
FAKE_IDEA_1_SUMMARY = "fake-summary-1：探索主题 t1 的独立路线 token1"
FAKE_IDEA_2_TITLE = "候选想法 2：面向空白 g2 的方法（fake）"


async def _setup_project(client, email="alice@example.com", name="forge-proj"):
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects", json={"name": name, "definition": DEFINITION}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


async def _seed_compiled_papers(project_id: str, n: int = 3) -> list[str]:
    async with get_sessionmaker()() as session:
        ids = []
        for i in range(n):
            paper = Paper(
                project_id=uuid.UUID(project_id),
                source="manual",
                title=f"Compiled paper {i}",
                abstract=f"Abstract of paper {i} about research agents.",
                tldr=f"论文 {i} 的一句话总结",
                relevance_score=0.9 - i * 0.01,
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


async def test_forge_full_pipeline(client, queue_stub):
    project_id, headers = await _setup_project(client)
    paper_ids = await _seed_compiled_papers(project_id, n=3)

    # 初始 forge/state：空
    resp = await client.get(f"/api/projects/{project_id}/forge/state", headers=headers)
    state = resp.json()
    assert state["running_voyage_id"] is None and state["last_run"] is None
    assert state["idea_counts"]["total"] == 0

    resp = await client.post(
        f"/api/projects/{project_id}/forge", json={"knobs": KNOBS}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    voyage = resp.json()
    assert voyage["kind"] == "idea_forge"
    assert voyage["budget"]["max_tokens"] == 2 * 20_000  # 预算从 knobs 派生
    run_id = voyage["id"]
    assert ("run_voyage", (run_id,), {}) in queue_stub.jobs

    # 同项目 forge/review 互斥 → 409
    resp = await client.post(
        f"/api/projects/{project_id}/forge", json={"knobs": KNOBS}, headers=headers
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "IDEA_VOYAGE_ALREADY_RUNNING"
    resp = await client.post(
        f"/api/projects/{project_id}/review/tournament", json={}, headers=headers
    )
    assert resp.status_code == 409

    # forge/state：running_voyage_id 指向进行中的航程
    resp = await client.get(f"/api/projects/{project_id}/forge/state", headers=headers)
    assert resp.json()["running_voyage_id"] == run_id

    engine, _bus = _make_engine()
    await engine.run(uuid.UUID(run_id))

    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "done", detail
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 7
    assert detail["steps"][0]["observation"]["papers"] == 3  # 上下文读到 3 篇 compiled
    assert detail["steps"][1]["observation"]["enabled"] == [
        "survey_gap",
        "concept_holes",
        "limitations",
        "trends",
    ]
    assert detail["steps"][2]["observation"]["gaps"] == 2  # 无概念/全文/近期趋势 → 仅综述 gap
    assert detail["steps"][3]["observation"]["generated"] == 2
    assert detail["steps"][4]["observation"]["succeeded"] == 2
    assert detail["steps"][5]["observation"]["dropped"] == 0
    assert detail["steps"][6]["observation"]["inserted"] == 2

    async with get_sessionmaker()() as session:
        ideas = (
            (await session.execute(select(Idea).where(Idea.project_id == uuid.UUID(project_id))))
            .scalars()
            .all()
        )
        assert len(ideas) == 2
        for idea in ideas:
            assert idea.status == "candidate"
            assert idea.elo_rating == 1200.0 and idea.matches == 0 and idea.wins == 0
            assert set(idea.scores) == {"novelty", "feasibility", "operability", "impact"}
            assert all(0 <= v <= 10 for v in idea.scores.values())
            assert set(idea.score_rationale) == set(idea.scores)
            assert sorted(idea.parent_paper_ids) == sorted(paper_ids)
            assert idea.embedding is not None and len(idea.embedding) == EMBEDDING_DIM
            assert "## 动机" in idea.content and "## 风险" in idea.content
            assert idea.depth == "sketch"
            assert idea.evidence and idea.evidence[0]["source"] == "signal"
        activity_kinds = {
            a.kind
            for a in (
                await session.execute(
                    select(Activity).where(Activity.project_id == uuid.UUID(project_id))
                )
            ).scalars()
        }
        assert {"forge.started", "forge.completed"} <= activity_kinds

    # ideas 列表（IdeaRead 契约形状：不含 content）
    resp = await client.get(f"/api/projects/{project_id}/ideas", headers=headers)
    items = resp.json()
    assert len(items) == 2
    assert set(items[0]) == {
        "id",
        "project_id",
        "title",
        "summary",
        "scores",
        "elo_rating",
        "status",
        "depth",
        "research_type",
        "created_at",
    }
    assert all(i["depth"] == "sketch" for i in items)
    for sort in ("elo", "-created_at", "score"):
        resp = await client.get(f"/api/projects/{project_id}/ideas?sort={sort}", headers=headers)
        assert resp.status_code == 200 and len(resp.json()) == 2
    resp = await client.get(f"/api/projects/{project_id}/ideas?status=candidate", headers=headers)
    assert len(resp.json()) == 2

    # idea 详情（IdeaDetail：content/parent_papers/score_rationale）
    idea_id = items[0]["id"]
    resp = await client.get(f"/api/ideas/{idea_id}", headers=headers)
    detail = resp.json()
    assert "## 方法概述" in detail["content"]
    assert sorted(detail["parent_paper_ids"]) == sorted(paper_ids)
    assert len(detail["parent_papers"]) == 3
    assert detail["parent_papers"][0]["title"].startswith("Compiled paper")
    assert detail["score_rationale"]["novelty"]

    # forge/state：完成后 last_run 与计数
    resp = await client.get(f"/api/projects/{project_id}/forge/state", headers=headers)
    state = resp.json()
    assert state["running_voyage_id"] is None
    assert state["last_run"]["voyage_id"] == run_id
    assert state["last_run"]["status"] == "done"
    assert state["last_run"]["finished_at"]
    counts = state["idea_counts"]
    assert counts["candidate"] == 2 and counts["total"] == 2


async def test_forge_dedup_drops_duplicates(client, queue_stub):
    """与库内既有 idea 语义重复（余弦超阈 + rerank 复核）的候选被丢弃并记录。"""
    project_id, headers = await _setup_project(client)
    await _seed_compiled_papers(project_id, n=1)
    async with get_sessionmaker()() as session:
        session.add(
            Idea(
                project_id=uuid.UUID(project_id),
                title=FAKE_IDEA_1_TITLE,  # 与 fake 生成的候选 1 完全同文 → 必重复
                summary=FAKE_IDEA_1_SUMMARY,
                status="candidate",
            )
        )
        await session.commit()

    resp = await client.post(
        f"/api/projects/{project_id}/forge", json={"knobs": KNOBS}, headers=headers
    )
    run_id = resp.json()["id"]
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(run_id))

    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "done", detail
    dedup_obs = detail["steps"][5]["observation"]
    assert dedup_obs["existing_compared"] == 1  # 既有 idea 现场补嵌后参与比对
    assert dedup_obs["dropped"] == 1
    dropped = dedup_obs["dropped_detail"][0]
    assert dropped["title"] == FAKE_IDEA_1_TITLE
    assert dropped["cosine"] > 0.9 and dropped["rerank_score"] >= 0.5
    assert detail["steps"][6]["observation"]["inserted"] == 1  # 只入库存活的候选 2

    async with get_sessionmaker()() as session:
        titles = (
            (
                await session.execute(
                    select(Idea.title).where(Idea.project_id == uuid.UUID(project_id))
                )
            )
            .scalars()
            .all()
        )
        assert sorted(titles) == sorted([FAKE_IDEA_1_TITLE, FAKE_IDEA_2_TITLE])


class _CrashOnFirstScore(FakeProvider):
    """模拟 worker 在第一次四维打分时被杀；同时按 marker 统计各类 forge 调用次数。"""

    def __init__(self) -> None:
        self.gap_calls = 0
        self.generate_calls = 0
        self.score_calls = 0
        self.crashed = False

    async def complete(self, messages, *, model, temperature=0.7, max_tokens=None):
        full = "\n".join(m.content for m in messages)
        if '"gaps"' in full:
            self.gap_calls += 1
        if '"ideas"' in full:
            self.generate_calls += 1
        if '"operability"' in full:
            self.score_calls += 1
            if not self.crashed:
                self.crashed = True
                raise asyncio.CancelledError("simulated worker kill")
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens
        )


async def test_forge_resume_does_not_regenerate(client, queue_stub):
    """打分途中被杀再 resume：gap 分析与候选生成不重复调 LLM（checkpoint 幂等）。"""
    project_id, headers = await _setup_project(client)
    await _seed_compiled_papers(project_id, n=2)
    resp = await client.post(
        f"/api/projects/{project_id}/forge", json={"knobs": KNOBS}, headers=headers
    )
    run_id = uuid.UUID(resp.json()["id"])

    provider = _CrashOnFirstScore()
    router = LLMRouter()
    router._providers[("fake", None, "")] = provider
    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=router)
    with pytest.raises(asyncio.CancelledError):
        await engine.run(run_id)
    assert provider.gap_calls == 1 and provider.generate_calls == 1

    # resume（同一 provider 计数）：gap/生成步骤已过 cursor，不再调 LLM
    engine2 = VoyageEngine(event_bus=RecordingBus(), llm_router=router)
    await engine2.resume(run_id)
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    assert resp.json()["status"] == "done"
    assert provider.gap_calls == 1
    assert provider.generate_calls == 1
    assert provider.score_calls == 3  # 崩溃 1 次 + resume 后 2 个 idea 各 1 次

    async with get_sessionmaker()() as session:
        count = len(
            (await session.execute(select(Idea.id).where(Idea.project_id == uuid.UUID(project_id))))
            .scalars()
            .all()
        )
        assert count == 2  # 不重复入库


async def test_forge_api_permissions(client, queue_stub):
    """非项目成员对 forge/ideas 一律 404（不泄露存在性）。"""
    project_id, headers = await _setup_project(client)
    await _seed_compiled_papers(project_id, n=1)
    resp = await client.post(
        f"/api/projects/{project_id}/forge", json={"knobs": KNOBS}, headers=headers
    )
    run_id = resp.json()["id"]
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(run_id))
    resp = await client.get(f"/api/projects/{project_id}/ideas", headers=headers)
    idea_id = resp.json()[0]["id"]

    token_b = await register_and_login(client, email="outsider@example.com")
    headers_b = {"Authorization": f"Bearer {token_b}"}
    for method, url, body in (
        ("post", f"/api/projects/{project_id}/forge", {"knobs": KNOBS}),
        ("get", f"/api/projects/{project_id}/forge/state", None),
        ("get", f"/api/projects/{project_id}/ideas", None),
        ("get", f"/api/ideas/{idea_id}", None),
        ("get", f"/api/ideas/{idea_id}/sessions", None),
        ("get", f"/api/projects/{project_id}/review/leaderboard", None),
        ("post", f"/api/projects/{project_id}/review/tournament", {}),
    ):
        resp = await getattr(client, method)(
            url, **({"json": body} if body is not None else {}), headers=headers_b
        )
        assert resp.status_code == 404, (method, url, resp.status_code)


async def test_collect_signals_concept_holes_and_trends(client, queue_stub):
    """确定性信号：method×problem 零共现概念对 + 近 90 天概念趋势（纯代码，无 LLM）。"""
    from app.agents.voyage.actions import ActionContext
    from app.agents.voyage.actions_ideas import forge_collect_signals
    from app.models.paper import Concept
    from app.models.voyage import VoyageRun

    project_id, headers = await _setup_project(client)
    paper_ids = await _seed_compiled_papers(project_id, n=4)

    async with get_sessionmaker()() as session:
        pid = uuid.UUID(project_id)
        method_a = Concept(project_id=pid, name="方法A", slug="m-a", category="method")
        method_b = Concept(project_id=pid, name="方法B", slug="m-b", category="method")
        problem_x = Concept(project_id=pid, name="问题X", slug="p-x", category="problem")
        session.add_all([method_a, method_b, problem_x])
        await session.flush()
        # 方法A 与 问题X 共现（paper0）；方法B 只出现在 paper1/2 → 方法B×问题X 零共现
        from app.models.paper import paper_concepts

        links = [
            (paper_ids[0], method_a.id),
            (paper_ids[0], problem_x.id),
            (paper_ids[1], method_b.id),
            (paper_ids[2], method_b.id),
            (paper_ids[3], problem_x.id),
        ]
        for paper_id, concept_id in links:
            await session.execute(
                paper_concepts.insert().values(paper_id=uuid.UUID(paper_id), concept_id=concept_id)
            )
        await session.commit()

        run = VoyageRun(
            kind="idea_forge",
            goal="signals",
            status="executing",
            cursor=0,
            project_id=pid,
            checkpoint={"params": {"knobs": {"signals": ["concept_holes", "trends"]}}},
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

    from app.core.llm.router import LLMRouter

    ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint))
    obs = await forge_collect_signals(ctx, {})
    signals = ctx.checkpoint["forge_signals"]
    holes = signals["concept_holes"]
    assert {k: holes[0][k] for k in ("method", "problem")} == {
        "method": "方法B",
        "problem": "问题X",
    }
    assert holes[0]["method_papers"] == 2 and holes[0]["problem_papers"] == 2
    # 方法A×问题X 有共现 → 不在空白列表
    assert all(h["method"] != "方法A" for h in holes)
    # 刚入库的论文都在 90 天窗口内 → 概念计数进趋势（阈值 ≥2）
    trend_names = {t["concept"] for t in signals["trends"]}
    assert {"方法B", "问题X"} <= trend_names
    assert obs["enabled"] == ["concept_holes", "trends"]
    # 幂等：重跑不重复采集
    obs2 = await forge_collect_signals(ctx, {})
    assert obs2["skipped"] is True
