"""wiki ingest 全流程测试：respx mock 文献 API + fake LLM，直接驱动 VoyageEngine。

覆盖：bootstrap 冷启动全链路（候选→雪球→打分→全文→编译→概念→水位线）、
并发 409、断点恢复不重复打分（fake LLM 调用计数）、增量续跑、每日 cron 选表。
"""

import asyncio
import uuid

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import func, select

from app.agents.voyage.engine import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.fake import FakeProvider
from app.core.llm.router import LLMRouter
from app.models.activity import Activity
from app.models.llm_config import LLMUsage
from app.models.paper import EMBEDDING_DIM, Concept, Paper, paper_concepts
from app.models.project import Project
from app.services import ingest as ingest_service
from app.services.literature import (
    ArxivClient,
    OpenAlexClient,
    SemanticScholarClient,
    reset_clients,
    set_clients,
)
from app.services.literature.pdf_extract import figure_path
from tests.conftest import RecordingBus, register_and_login

DEFINITION = {
    "statement": "自动化科研 agent 的方法研究",
    "questions": ["如何让 LLM agent 自主提出并验证研究想法？"],
    "rubric": [{"name": "novelty", "description": "新颖性", "weight": 1.0}],
    "anchor_papers": [{"title": "Anchor", "arxiv_id": "2404.11111"}],
    "keywords": {
        "arxiv_categories": ["cs.LG"],
        "include": ["autonomous research agent"],
    },
    "cadence": "daily",
}

KNOBS = {
    "months_back": 6,
    "max_papers": 10,
    "relevance_threshold": 0.6,
    "snowball_depth": 1,
    "compile_top_n": 5,
}

ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2406.00001v1</id>
    <title>Autonomous Research Agents via Reinforcement Learning</title>
    <summary>We build autonomous research agents with RL.</summary>
    <published>2026-06-01T00:00:00Z</published>
    <updated>2026-06-01T00:00:00Z</updated>
    <author><name>Alice</name></author>
    <category term="cs.LG"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2406.00002v1</id>
    <title>LLM Scientist Benchmark Suite</title>
    <summary>A benchmark suite for LLM scientists.</summary>
    <published>2026-05-20T00:00:00Z</published>
    <updated>2026-05-20T00:00:00Z</updated>
    <author><name>Bob</name></author>
    <category term="cs.LG"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2406.00003v1</id>
    <title>An irrelevant study of basket weaving</title>
    <summary>Nothing to do with agents (irrelevant).</summary>
    <published>2026-05-10T00:00:00Z</published>
    <updated>2026-05-10T00:00:00Z</updated>
    <author><name>Carol</name></author>
    <category term="cs.LG"/>
  </entry>
</feed>
"""

S2_ANCHOR_REFERENCES = {
    "data": [
        {
            "citedPaper": {
                "paperId": "s2snowball",
                "title": "Snowballed Agent Planning Paper",
                "abstract": "Planning methods for research agents.",
                "year": 2026,
                "venue": "ICML",
                "externalIds": {"ArXiv": "2405.00004"},
                "authors": [{"name": "Dave"}],
            }
        }
    ]
}


def _make_image_bytes(width: int, height: int) -> bytes:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height))
    pix.clear_with(90)
    return pix.tobytes("png")


def _make_pdf_bytes() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Full text: research agents with reinforcement learning.")
    # 嵌入一大一小两张图：大图为候选图，小图（<200×150）被尺寸过滤
    page.insert_image(pymupdf.Rect(72, 100, 272, 250), stream=_make_image_bytes(400, 300))
    page.insert_image(pymupdf.Rect(72, 260, 122, 300), stream=_make_image_bytes(100, 80))
    data = doc.tobytes()
    doc.close()
    return data


@pytest_asyncio.fixture
async def wiki_mocks(app):
    """离线文献环境：respx mock 三个外部 API + fakeredis 缓存 + 零限速客户端。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    set_clients(
        arxiv=ArxivClient(redis=redis, min_interval=0),
        s2=SemanticScholarClient(redis=redis, api_key="", rate=10_000, backoff_base=0.0),
        openalex=OpenAlexClient(redis=redis, mailto="test@example.org"),
    )
    pdf_bytes = _make_pdf_bytes()
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
            return_value=httpx.Response(200, text=ARXIV_FEED)
        )
        router.get(
            url__regex=r".*semanticscholar\.org/graph/v1/paper/arXiv:2404\.11111/references.*"
        ).mock(return_value=httpx.Response(200, json=S2_ANCHOR_REFERENCES))
        router.get(url__regex=r".*semanticscholar\.org/graph/v1/paper/.*").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        router.get(url__regex=r"https://arxiv\.org/pdf/.*").mock(
            return_value=httpx.Response(200, content=pdf_bytes)
        )
        yield router
    reset_clients()
    await redis.aclose()


async def _setup_project(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects", json={"name": "wiki-proj", "definition": DEFINITION}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


def _make_engine() -> tuple[VoyageEngine, RecordingBus]:
    bus = RecordingBus()
    return VoyageEngine(event_bus=bus, llm_router=LLMRouter()), bus


async def _relevance_call_count() -> int:
    async with get_sessionmaker()() as session:
        stmt = select(func.count()).where(LLMUsage.stage == "relevance")
        return int((await session.execute(stmt)).scalar_one())


async def test_bootstrap_full_pipeline(client, queue_stub, wiki_mocks):
    project_id, headers = await _setup_project(client)

    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage = resp.json()
    assert voyage["kind"] == "wiki_bootstrap"
    assert voyage["budget"]["max_tokens"] == 10 * 20_000  # 预算从 knobs 派生
    run_id = voyage["id"]
    assert ("run_voyage", (run_id,), {}) in queue_stub.jobs

    # 同项目并发互斥 → 409
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "INGEST_ALREADY_RUNNING"

    # ingest/state：running_voyage_id 指向进行中的航程
    resp = await client.get(f"/api/projects/{project_id}/ingest/state", headers=headers)
    assert resp.json()["running_voyage_id"] == run_id

    engine, _bus = _make_engine()
    await engine.run(uuid.UUID(run_id))

    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "done", detail
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 7
    obs0 = detail["steps"][0]["observation"]
    assert obs0["found"] == 3 and obs0["inserted"] == 3
    assert detail["steps"][1]["observation"]["inserted"] == 1  # 雪球 1 篇

    async with get_sessionmaker()() as session:
        papers = (
            (await session.execute(select(Paper).where(Paper.project_id == uuid.UUID(project_id))))
            .scalars()
            .all()
        )
        assert len(papers) == 4  # 3 arXiv 候选 + 1 雪球
        by_status = {}
        for p in papers:
            by_status.setdefault(p.status, []).append(p)
        assert len(by_status.get("excluded", [])) == 1  # "irrelevant" 论文被排除
        assert by_status["excluded"][0].arxiv_id == "2406.00003"
        compiled = by_status.get("compiled", [])
        assert len(compiled) == 3
        for p in compiled:
            assert p.relevance_score is not None and p.relevance_score >= 0.6
            assert p.scored_at is not None and p.compiled_at is not None
            assert p.tldr
            assert "[[Agent]]" in p.wiki_content  # 双链
            assert p.full_text_path and p.pdf_path  # PDF 已抽全文
            assert p.embedding is not None and len(p.embedding) == EMBEDDING_DIM
            # 管线顺带提取论文图（小图被滤），compile 后由 fake VLM 注释
            assert p.figures == [
                {
                    "index": 0,
                    "page": 1,
                    "width": 400,
                    "height": 300,
                    "caption": "（fake）图注",
                    "kind": "method",
                    "important": True,
                }
            ]
            assert figure_path(str(p.id), 0).exists()

        concepts = (
            (
                await session.execute(
                    select(Concept).where(Concept.project_id == uuid.UUID(project_id))
                )
            )
            .scalars()
            .all()
        )
        names = {c.name for c in concepts}
        assert names == {"Agent", "强化学习"}
        for c in concepts:
            assert c.definition and c.slug
            assert c.category == "method"
        links = int(
            (await session.execute(select(func.count()).select_from(paper_concepts))).scalar_one()
        )
        assert links == 6  # 3 篇编译论文 × 2 概念

        project = await session.get(Project, uuid.UUID(project_id))
        assert project.ingest_state["watermark"]
        assert project.ingest_state["last_run"]["voyage_id"] == run_id

        activity_kinds = {
            a.kind
            for a in (
                await session.execute(
                    select(Activity).where(Activity.project_id == uuid.UUID(project_id))
                )
            ).scalars()
        }
        assert {"ingest.started", "ingest.completed"} <= activity_kinds

    # ingest/state：完成后的水位线与计数
    resp = await client.get(f"/api/projects/{project_id}/ingest/state", headers=headers)
    state = resp.json()
    assert state["watermark"]
    assert state["running_voyage_id"] is None
    assert state["last_run"]["voyage_id"] == run_id
    assert state["last_run"]["status"] == "done"
    counts = state["paper_counts"]
    assert counts["compiled"] == 3 and counts["excluded"] == 1 and counts["total"] == 4

    # papers API 上能看到编译结果
    resp = await client.get(f"/api/projects/{project_id}/papers?status=compiled", headers=headers)
    body = resp.json()
    assert body["total"] == 3
    assert all(item["has_wiki"] for item in body["items"])

    # 增量续跑：水位线窗口 + 全量去重，不产生新论文
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "incremental", "knobs": KNOBS},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage2 = resp.json()
    assert voyage2["kind"] == "wiki_ingest"
    engine2, _ = _make_engine()
    await engine2.run(uuid.UUID(voyage2["id"]))
    resp = await client.get(f"/api/voyages/{voyage2['id']}", headers=headers)
    detail2 = resp.json()
    assert detail2["status"] == "done"
    assert detail2["steps"][0]["observation"]["mode"] == "incremental"
    assert detail2["steps"][0]["observation"]["inserted"] == 0  # 去重
    async with get_sessionmaker()() as session:
        total = int(
            (
                await session.execute(
                    select(func.count()).where(Paper.project_id == uuid.UUID(project_id))
                )
            ).scalar_one()
        )
        assert total == 4


class _CrashOnNthRelevance(FakeProvider):
    """模拟 worker 在第 N 次相关性打分时被杀（CancelledError 不被逐篇 try/except 吞掉）。"""

    def __init__(self, crash_at: int) -> None:
        self.relevance_calls = 0
        self.crash_at = crash_at

    async def complete(self, messages, *, model, temperature=0.7, max_tokens=None, images=None):
        if any('"score"' in m.content for m in messages):
            self.relevance_calls += 1
            if self.relevance_calls == self.crash_at:
                raise asyncio.CancelledError("simulated worker kill")
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, images=images
        )


async def test_resume_does_not_rescore(client, queue_stub, wiki_mocks):
    """跑一半 kill 再 resume：已打分论文不重复调 LLM（按 LLMUsage 计数断言）。"""
    project_id, headers = await _setup_project(client)
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    run_id = uuid.UUID(resp.json()["id"])

    # 第 2 次打分 "被杀"。打分是有界并发（_LLM_CONCURRENCY=5 ≥ 4 篇候选），4 个任务
    # 并发各自打分：其中 1 个（第 2 次 LLM 调用）抛 CancelledError，其余 3 个照常完成并
    # 逐篇 commit。故崩溃后恰好 3 篇落库、1 篇仍是 candidate（下次续跑补打这 1 篇）。
    crashing_router = LLMRouter()
    crashing_router._providers[("fake", None, "")] = _CrashOnNthRelevance(crash_at=2)
    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=crashing_router)
    with pytest.raises(asyncio.CancelledError):
        await engine.run(run_id)

    assert await _relevance_call_count() == 3  # 崩溃前 3 篇成功打分（并发，非串行的 1）
    async with get_sessionmaker()() as session:
        scored = (
            await session.execute(
                select(func.count()).where(
                    Paper.project_id == uuid.UUID(project_id),
                    Paper.status.in_(("scored", "excluded")),
                )
            )
        ).scalar_one()
        assert int(scored) == 3  # 逐篇 commit：崩溃前的进度已落库

    # resume：从断点续跑到 done，总打分调用数 == 论文数（无重复）
    engine2, _ = _make_engine()
    await engine2.resume(run_id)
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    assert resp.json()["status"] == "done"
    assert await _relevance_call_count() == 4  # 4 篇论文各打分一次

    async with get_sessionmaker()() as session:
        statuses = (
            (
                await session.execute(
                    select(Paper.status).where(Paper.project_id == uuid.UUID(project_id))
                )
            )
            .scalars()
            .all()
        )
        assert sorted(statuses) == ["compiled", "compiled", "compiled", "excluded"]


class _BreakOneRelevance(FakeProvider):
    """让某一篇（标题含 marker）的相关性打分返回坏 JSON：验证并发下单篇失败被隔离，
    其余并发任务照常打分（failed 结构不变、最终计数与串行一致）。"""

    def __init__(self, break_marker: str) -> None:
        self.break_marker = break_marker

    async def complete(self, messages, *, model, temperature=0.7, max_tokens=None, images=None):
        full = "\n".join(m.content for m in messages)
        if '"score"' in full and self.break_marker in full:
            from app.core.llm.base import CompletionResult

            return CompletionResult(content="(not json)", model=model, finish_reason="stop")
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, images=images
        )


async def test_concurrent_scoring_failure_isolation(client, queue_stub, wiki_mocks):
    """并发打分中一篇解析失败进 failed，不拖累其余；全程走完到 done。"""
    project_id, headers = await _setup_project(client)
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    run_id = uuid.UUID(resp.json()["id"])

    router = LLMRouter()
    # "Benchmark" 命中「LLM Scientist Benchmark Suite」这一篇 → 该篇打分返回坏 JSON
    router._providers[("fake", None, "")] = _BreakOneRelevance(break_marker="Benchmark")
    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=router)
    await engine.run(run_id)

    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "done", detail
    score_obs = detail["steps"][2]["observation"]  # 0 检索 / 1 雪球 / 2 打分
    assert len(score_obs["failed"]) == 1  # 恰好坏掉的那一篇进 failed
    assert score_obs["failed"][0]["error"].startswith("ValueError")
    assert score_obs["succeeded"] == 3  # 其余 3 篇并发打分照常完成（4 候选 − 1 失败）
    assert score_obs["processed"] == 4

    async with get_sessionmaker()() as session:
        by_status: dict[str, int] = {}
        for status in (
            (
                await session.execute(
                    select(Paper.status).where(Paper.project_id == uuid.UUID(project_id))
                )
            )
            .scalars()
            .all()
        ):
            by_status[status] = by_status.get(status, 0) + 1
        # 失败打分的那篇仍是 candidate（下次续跑会重试），其余照常推进
        assert by_status.get("candidate", 0) == 1
        assert by_status.get("excluded", 0) == 1  # irrelevant 篮子编织论文
        assert by_status.get("compiled", 0) == 2  # 两篇通过阈值的论文成功编译


async def test_sparse_definition_bootstrap_smoke(client, queue_stub, wiki_mocks):
    """稀疏 definition（只有 statement）也能跑通 bootstrap 全链路（默认 cs.* 分类兜底）。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects",
        json={"name": "sparse-proj", "definition": {"statement": "自动化科研 agent 的方法研究"}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]

    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(run_id))

    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "done", detail
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 7
    assert detail["steps"][0]["observation"]["inserted"] == 3  # 默认分类兜底后仍能检索

    async with get_sessionmaker()() as session:
        statuses = (
            (
                await session.execute(
                    select(Paper.status).where(Paper.project_id == uuid.UUID(project_id))
                )
            )
            .scalars()
            .all()
        )
        # 无锚点论文 → 雪球 0 篇；3 候选：2 编译 + 1 排除（无 rubric 时打分只用 statement）
        assert sorted(statuses) == ["compiled", "compiled", "excluded"]


async def test_daily_cron_project_selection(client, queue_stub, wiki_mocks):
    """cadence=daily 且已 bootstrap（有水位线）的项目才进入每日增量。"""
    project_id, headers = await _setup_project(client)
    async with get_sessionmaker()() as session:
        due = await ingest_service.find_due_daily_projects(session)
        assert due == []  # 尚未 bootstrap（无水位线）

    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    run_id = uuid.UUID(resp.json()["id"])
    engine, _ = _make_engine()
    await engine.run(run_id)

    async with get_sessionmaker()() as session:
        due = await ingest_service.find_due_daily_projects(session)
        assert [p.id for p in due] == [uuid.UUID(project_id)]
