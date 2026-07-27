"""wiki ingest 全流程测试：respx mock 文献 API + fake LLM，直接驱动 VoyageEngine。

覆盖：检索模式全链路（检索→打分→全文→编译→概念→记录进度）、锚点扩展模式、
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
from app.core.llm.fake import EMBEDDING_DIM, FakeProvider
from app.core.llm.router import LLMRouter
from app.models.activity import Activity
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.llm_config import LLMUsage
from app.models.paper import Paper, new_paper, paper_concepts
from app.models.user import User
from app.models.voyage import VoyageRun
from app.services import ingest as ingest_service
from app.services.literature import (
    ArxivClient,
    OpenAlexClient,
    SemanticScholarClient,
    reset_clients,
    set_clients,
)
from app.services.literature.pdf_extract import figure_path
from tests.conftest import (
    RecordingBus,
    make_project_with_library,
    project_concepts,
    project_paper_rows,
    register_and_login,
    wiki_of,
)
from tests.vector_helpers import get_paper_vector

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

# 分类 RSS「新鲜源」样例（cs.LG /new）：绕开关键词检索索引 3-5 天滞后。
# include 关键词为 "autonomous research agent"（见 DEFINITION）。
# - 2607.30001 (new)：连字符变体 "Autonomous-Research Agents" 应被宽松过滤命中 → 入库
# - 2607.30002 (cross)：cross 也接纳，v2 版本号被 normalize 去掉 → 入库
# - 2607.30003 (replace) / 2607.30004 (replace-cross)：旧论文更新 → 解析时跳过
# - 2607.30005 (new)：与关键词无关 → 宽松过滤滤除
# - 2406.00001 (new)：arxiv_id 与 bootstrap 已入库论文相同 → 三方去重命中，不重插
ARXIV_RSS = """<?xml version='1.0' encoding='UTF-8'?>
<rss xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:dc="http://purl.org/dc/elements/1.1/" \
xmlns:atom="http://www.w3.org/2005/Atom" version="2.0">
  <channel>
    <title>cs.LG updates on arXiv.org</title>
    <item>
      <title>Autonomous-Research Agents: A Fresh Result</title>
      <link>https://arxiv.org/abs/2607.30001</link>
      <description>arXiv:2607.30001v1 Announce Type: new
Abstract: Fresh work on autonomous research agents announced today.</description>
      <guid isPermaLink="false">oai:arXiv.org:2607.30001v1</guid>
      <category>cs.LG</category>
      <pubDate>Mon, 20 Jul 2026 00:00:00 -0400</pubDate>
      <arxiv:announce_type>new</arxiv:announce_type>
      <dc:creator>Alice Fresh</dc:creator>
    </item>
    <item>
      <title>Cross Study of Planning</title>
      <link>https://arxiv.org/abs/2607.30002</link>
      <description>arXiv:2607.30002v2 Announce Type: cross
Abstract: We present autonomous research agent methods, cross-listed.</description>
      <guid isPermaLink="false">oai:arXiv.org:2607.30002v2</guid>
      <category>cs.LG</category>
      <pubDate>Mon, 20 Jul 2026 00:00:00 -0400</pubDate>
      <arxiv:announce_type>cross</arxiv:announce_type>
      <dc:creator>Bob Cross</dc:creator>
    </item>
    <item>
      <title>Autonomous Research Agents Revisited</title>
      <link>https://arxiv.org/abs/2607.30003</link>
      <description>arXiv:2607.30003v3 Announce Type: replace
Abstract: A revised version about autonomous research agents.</description>
      <guid isPermaLink="false">oai:arXiv.org:2607.30003v3</guid>
      <category>cs.LG</category>
      <pubDate>Mon, 20 Jul 2026 00:00:00 -0400</pubDate>
      <arxiv:announce_type>replace</arxiv:announce_type>
      <dc:creator>Carol Old</dc:creator>
    </item>
    <item>
      <title>Old Cross About Agents</title>
      <link>https://arxiv.org/abs/2607.30004</link>
      <description>arXiv:2607.30004v2 Announce Type: replace-cross
Abstract: Revised cross-listed autonomous research agent paper.</description>
      <guid isPermaLink="false">oai:arXiv.org:2607.30004v2</guid>
      <category>cs.LG</category>
      <pubDate>Mon, 20 Jul 2026 00:00:00 -0400</pubDate>
      <arxiv:announce_type>replace-cross</arxiv:announce_type>
      <dc:creator>Dave Old</dc:creator>
    </item>
    <item>
      <title>Basket Weaving Handbook</title>
      <link>https://arxiv.org/abs/2607.30005</link>
      <description>arXiv:2607.30005v1 Announce Type: new
Abstract: Nothing to do with the topic here.</description>
      <guid isPermaLink="false">oai:arXiv.org:2607.30005v1</guid>
      <category>cs.LG</category>
      <pubDate>Mon, 20 Jul 2026 00:00:00 -0400</pubDate>
      <arxiv:announce_type>new</arxiv:announce_type>
      <dc:creator>Eve Weaver</dc:creator>
    </item>
    <item>
      <title>Autonomous Research Agents via Reinforcement Learning</title>
      <link>https://arxiv.org/abs/2406.00001</link>
      <description>arXiv:2406.00001v1 Announce Type: new
Abstract: Duplicate of an already ingested paper (autonomous research agent).</description>
      <guid isPermaLink="false">oai:arXiv.org:2406.00001v1</guid>
      <category>cs.LG</category>
      <pubDate>Mon, 20 Jul 2026 00:00:00 -0400</pubDate>
      <arxiv:announce_type>new</arxiv:announce_type>
      <dc:creator>Alice</dc:creator>
    </item>
  </channel>
</rss>
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
    # P9c：课题不再自动建库——显式配一个带 DEFINITION 的 active 起源库并关联。
    project_id, _library_id = await make_project_with_library(
        client, headers, name="wiki-proj", definition=DEFINITION
    )
    return project_id, headers


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
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 6
    obs0 = detail["steps"][0]["observation"]
    assert obs0["found"] == 3 and obs0["inserted"] == 3

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        assert len(rows) == 3  # 检索模式只有 arXiv 候选，引文扩展是另一个模式
        by_status = {}
        for p, m in rows:
            by_status.setdefault(m.status, []).append((p, m))
        assert len(by_status.get("excluded", [])) == 1  # "irrelevant" 论文被排除
        assert by_status["excluded"][0][0].arxiv_id == "2406.00003"
        compiled_rows = by_status.get("compiled", [])
        assert len(compiled_rows) == 2
        for p, m in compiled_rows:
            assert m.relevance_score is not None and m.relevance_score >= 0.6
            assert m.scored_at is not None
            wiki = await wiki_of(session, paper_id=m.paper_id)
            assert wiki is not None and wiki.model == "fake-default"  # 编译模型落解读行
            assert p.tldr
            assert "[[Agent]]" in wiki.content  # 双链
            assert p.full_text_path and p.pdf_path  # PDF 已抽全文
            vector = await get_paper_vector(session, p.id)
            assert vector is not None and len(vector) == EMBEDDING_DIM
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

        concepts = await project_concepts(session, project_id=project_id)
        names = {c.name for c in concepts}
        assert names == {"Agent", "强化学习"}
        for c in concepts:
            assert c.definition and c.slug
            assert c.category == "method"
        links = int(
            (await session.execute(select(func.count()).select_from(paper_concepts))).scalar_one()
        )
        assert links == 4  # 2 篇编译论文 × 2 概念

        # P8a：水位线权威源在库（library.ingest_state），不再写起源课题
        from app.services.libraries import get_library_for_project

        library = await get_library_for_project(session, uuid.UUID(project_id))
        assert library.ingest_state["watermark"]
        assert library.ingest_state["last_run"]["voyage_id"] == run_id

        # ingest 活动流归库（任务也归库）：按 library_id 查，不再挂课题
        activity_kinds = {
            a.kind
            for a in (
                await session.execute(
                    select(Activity).where(Activity.library_id == library.id)
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
    assert counts["compiled"] == 2 and counts["excluded"] == 1 and counts["total"] == 3

    # papers API 上能看到编译结果
    resp = await client.get(f"/api/projects/{project_id}/papers?status=compiled", headers=headers)
    body = resp.json()
    assert body["total"] == 2
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
    # 增量的候选来自每日论文池，不再检索 arXiv，所以没有时间窗这回事
    obs2 = detail2["steps"][0]["observation"]
    assert obs2["source"] == "daily_feed"
    assert "window_since" not in obs2
    assert obs2["feed_total"] == 0  # 本测试没往每日池放东西
    async with get_sessionmaker()() as session:
        assert len(await project_paper_rows(session, project_id=project_id)) == 3


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

    assert await _relevance_call_count() == 2  # 崩溃前 2 篇成功打分（并发，非串行的 1）
    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        scored = sum(1 for _, m in rows if m.status in ("scored", "excluded"))
        assert scored == 2  # 逐篇 commit：崩溃前的进度已落库

    # resume：从断点续跑到 done，总打分调用数 == 论文数（无重复）
    engine2, _ = _make_engine()
    await engine2.resume(run_id)
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    assert resp.json()["status"] == "done"
    assert await _relevance_call_count() == 3  # 3 篇论文各打分一次

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        assert sorted(m.status for _, m in rows) == ["compiled", "compiled", "excluded"]


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
    score_obs = detail["steps"][1]["observation"]  # 0 检索 / 1 打分
    assert len(score_obs["failed"]) == 1  # 恰好坏掉的那一篇进 failed
    assert score_obs["failed"][0]["error"].startswith("ValueError")
    assert score_obs["succeeded"] == 2  # 其余 2 篇并发打分照常完成（3 候选 − 1 失败）
    assert score_obs["processed"] == 3

    async with get_sessionmaker()() as session:
        by_status: dict[str, int] = {}
        for _, m in await project_paper_rows(session, project_id=project_id):
            by_status[m.status] = by_status.get(m.status, 0) + 1
        # 失败打分的那篇仍是 candidate（下次续跑会重试），其余照常推进
        assert by_status.get("candidate", 0) == 1
        assert by_status.get("excluded", 0) == 1  # irrelevant 篮子编织论文
        assert by_status.get("compiled", 0) == 1  # 通过阈值且未失败的那篇成功编译


async def test_sparse_definition_bootstrap_smoke(client, queue_stub, wiki_mocks):
    """稀疏 definition（只有 statement）也能跑通 bootstrap 全链路（默认 cs.* 分类兜底）。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _library_id = await make_project_with_library(
        client,
        headers,
        name="sparse-proj",
        definition={"statement": "自动化科研 agent 的方法研究"},
    )

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
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 6
    assert detail["steps"][0]["observation"]["inserted"] == 3  # 默认分类兜底后仍能检索

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        # 3 候选：2 编译 + 1 排除（无 rubric 时打分只用 statement）
        assert sorted(m.status for _, m in rows) == ["compiled", "compiled", "excluded"]


async def test_incremental_pulls_from_daily_feed_without_arxiv(client, queue_stub, wiki_mocks):
    """增量同步只吃每日论文池——**arXiv 全线 429 也要跑通**。

    这是整个改动的意义所在：生产上三个库同步就是死在检索接口的 429 上。所以这里
    先 bootstrap（走 arXiv，正常响应），再把所有 arXiv 路由改成 429，然后跑增量，
    要求它照样 done、照样把每日池里的新论文收进来。
    """
    import datetime as dt

    from app.models.daily_feed import DailyFeedEntry

    project_id, headers = await _setup_project(client)

    # bootstrap 建立水位线与存量论文（此时 arXiv 是通的）
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    # 每日池里放两篇新论文 + 一篇与存量重复的（去重要挡掉它）
    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        existing_aid = next(p.arxiv_id for p, _ in rows if p.arxiv_id)
        existing_id = next(p.id for p, _ in rows if p.arxiv_id == existing_aid)
        feed_ids = []
        for i, title in enumerate(["Feed Agent Paper", "Feed Distillation Paper"]):
            paper = new_paper(
                source="arxiv",
                arxiv_id=f"2607.9000{i}",
                title=title,
                abstract="research agents and planning",
                year=2026,
                published_at=dt.datetime(2026, 7, 20, tzinfo=dt.UTC),
            )
            session.add(paper)
            await session.flush()
            feed_ids.append(paper.id)
            session.add(
                DailyFeedEntry(
                    paper_id=paper.id, feed_date=dt.date(2026, 7, 20), primary_category="cs.AI"
                )
            )
        # 已在库的论文也进池：必须被去重挡住，不能重复送打分
        session.add(
            DailyFeedEntry(
                paper_id=existing_id, feed_date=dt.date(2026, 7, 20), primary_category="cs.AI"
            )
        )
        await session.commit()

    # arXiv 全线限流：检索、RSS、按 id 取元数据统统 429
    arxiv_route = wiki_mocks.get(url__regex=r"https://(export\.)?arxiv\.org/.*").mock(
        return_value=httpx.Response(429)
    )
    rss_route = wiki_mocks.get(url__regex=r"https://rss\.arxiv\.org/.*").mock(
        return_value=httpx.Response(429)
    )

    resp2 = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "incremental", "knobs": KNOBS},
        headers=headers,
    )
    engine2, _ = _make_engine()
    await engine2.run(uuid.UUID(resp2.json()["id"]))

    detail = (await client.get(f"/api/voyages/{resp2.json()['id']}", headers=headers)).json()
    assert detail["status"] == "done", detail

    obs = detail["steps"][0]["observation"]
    assert obs["source"] == "daily_feed"
    assert obs["mode"] == "incremental"
    assert obs["feed_total"] == 3  # 池里 3 条（含 1 条与存量重复）
    assert obs["already_in_library"] == 1  # 重复那条被去重挡下，不重复打分
    assert obs["inserted"] == 2

    # 候选来源不再有 RSS，那套字段整体消失
    assert "rss_found" not in obs

    # 第一步一次 arXiv 都没打（PDF 下载在后续步骤，与候选来源无关）
    assert rss_route.call_count == 0

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        by_aid = {p.arxiv_id: m.status for p, m in rows}
        assert "2607.90000" in by_aid
        assert "2607.90001" in by_aid
        # 与存量重复的没有产生第二条成员行
        assert sum(1 for p, _ in rows if p.arxiv_id == existing_aid) == 1

    assert arxiv_route.call_count >= 0  # 保留句柄，避免 respx 未使用路由告警


# ---- 最大化模式（knobs.unlimited）：不限篇数 + 不限预算 ----

# 故意保留很小的 max_papers/compile_top_n：unlimited=True 时它们必须被忽略
UNLIMITED_KNOBS = {**KNOBS, "unlimited": True}

# 候选数 > _MAX_CANDIDATES_CAP=200，同时远超 compile 限 min(compile_top_n=5, max_papers=10)，
# 三处旧截断任一残留都会让断言失败
_BIG_N = 210


def _atom_feed(entries_xml: list[str]) -> str:
    body = "".join(entries_xml)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        f'xmlns:arxiv="http://arxiv.org/schemas/atom">{body}</feed>'
    )


def _big_entry(i: int) -> str:
    day = (i % 27) + 1
    return f"""
  <entry>
    <id>http://arxiv.org/abs/2606.{10000 + i}v1</id>
    <title>Autonomous Research Agent Study {i}</title>
    <summary>Deterministic study {i} of autonomous research agents.</summary>
    <published>2026-06-{day:02d}T00:00:00Z</published>
    <updated>2026-06-{day:02d}T00:00:00Z</updated>
    <author><name>Author {i}</name></author>
    <category term="cs.LG"/>
  </entry>"""


_BIG_ENTRIES = [_big_entry(i) for i in range(_BIG_N)]


@pytest_asyncio.fixture
async def wiki_mocks_big(app):
    """同 wiki_mocks，但 arXiv 检索按 start/max_results 分页返回 210 条候选（验证
    unlimited 模式下自动翻页抓全量、各步不被 200/top_n 截断）。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    set_clients(
        arxiv=ArxivClient(redis=redis, min_interval=0),
        s2=SemanticScholarClient(redis=redis, api_key="", rate=10_000, backoff_base=0.0),
        openalex=OpenAlexClient(redis=redis, mailto="test@example.org"),
    )
    pdf_bytes = _make_pdf_bytes()

    def _paged_arxiv(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("id_list"):  # fetch_by_ids（日期回填）：返回空 feed
            return httpx.Response(200, text=_atom_feed([]))
        start = int(params.get("start") or 0)
        max_results = int(params.get("max_results") or 100)
        return httpx.Response(200, text=_atom_feed(_BIG_ENTRIES[start : start + max_results]))

    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
            side_effect=_paged_arxiv
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


def test_unlimited_knobs_and_budget_semantics():
    """unlimited 缺省 False、可与显式篇数共存；预算给 None（引擎对 falsy 跳过检查）。"""
    from app.agents.voyage.actions_wiki import resolve_knobs
    from app.agents.voyage.engine import VoyageEngine
    from app.models.voyage import VoyageRun
    from app.schemas.ingest import IngestKnobs

    assert IngestKnobs().unlimited is False  # 向后兼容缺省
    knobs = IngestKnobs(unlimited=True, max_papers=10, compile_top_n=5)
    assert knobs.unlimited is True and knobs.max_papers == 10 and knobs.compile_top_n == 5

    # 预算：unlimited → max_tokens=None；默认模式派生公式不变
    assert ingest_service.derive_budget(knobs) == {"max_tokens": None}
    assert ingest_service.derive_budget(IngestKnobs()) == {"max_tokens": 50 * 20_000}

    # 引擎语义：max_tokens 为 None（falsy）不触发预算暂停，用量再大也不算超限
    run = VoyageRun(
        kind="wiki_bootstrap",
        goal="g",
        budget={"max_tokens": None},
        usage={"total_tokens": 10**9},
    )
    assert VoyageEngine._budget_exceeded(run) is False

    # resolve_knobs 透传 unlimited（缺省补 False）
    assert resolve_knobs({"unlimited": True})["unlimited"] is True
    assert resolve_knobs({})["unlimited"] is False
    assert resolve_knobs(None)["unlimited"] is False


async def test_unlimited_bootstrap_uncapped(client, queue_stub, wiki_mocks_big):
    """unlimited 全链路：210 候选（>200 硬顶）全量入库→全量打分→全量抽取编译，
    不被 max_papers/compile_top_n 截断，预算不触发暂停，任务跑到 done。"""
    project_id, headers = await _setup_project(client)

    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": UNLIMITED_KNOBS},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage = resp.json()
    assert voyage["budget"]["max_tokens"] is None  # 不限预算

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage["id"]))

    detail = (await client.get(f"/api/voyages/{voyage['id']}", headers=headers)).json()
    assert detail["status"] == "done", detail  # 未因预算暂停
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 6

    # 检索：分页抓到全部 210 条（旧逻辑 limit=min(200, 10*3)=30）
    obs0 = detail["steps"][0]["observation"]
    assert obs0["found"] == _BIG_N and obs0["inserted"] == _BIG_N
    # 检索模式不含引文扩展（那是 snowball 模式），所以总数就是检索到的篇数
    total = _BIG_N
    # 打分：210 篇全部处理（旧逻辑截 200）
    score_obs = detail["steps"][1]["observation"]
    assert score_obs["processed"] == total and score_obs["succeeded"] == total
    assert score_obs["excluded"] == 0  # 全部相关（fake 打 0.88 ≥ 0.6）
    # 抽取/编译：210 篇全部进入（旧逻辑截 min(compile_top_n=5, max_papers=10)=5）
    assert detail["steps"][2]["observation"]["processed"] == total
    compile_obs = detail["steps"][3]["observation"]
    assert compile_obs["processed"] == total and compile_obs["succeeded"] == total

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        compiled = sum(1 for _, m in rows if m.status == "compiled")
        assert compiled == total  # 全部编译落库，无一截断


async def test_project_backed_library_is_due_for_daily_cron(client, queue_stub, wiki_mocks):
    """有起源课题的库也走同一条按库选表的路径；未建库（无水位线）时不选。

    同步节奏不再参与判断——每日论文池只留一周，比「每天」更稀疏的节奏必然漏抓。
    """
    project_id, headers = await _setup_project(client)
    async with get_sessionmaker()() as session:
        assert await ingest_service.find_due_daily_libraries(session) == []  # 尚未 bootstrap

    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    async with get_sessionmaker()() as session:
        due = await ingest_service.find_due_daily_libraries(session)
        assert len(due) == 1
        assert due[0].project_id == uuid.UUID(project_id)


async def test_cadence_no_longer_excludes_a_library(client, queue_stub, wiki_mocks):
    """存量库里遗留的 cadence=weekly/manual 不再让它掉出每日同步。

    这些库以前压根没有 cron，只能人工点；配上每日池 7 天保留期就是永久漏抓。
    """
    token = await register_and_login(client, email="legacy-cadence@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("legacy-cadence@example.com")
    library_id = await _create_standalone_library(client, headers, name="独立库-周更")

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    # 模拟存量数据：definition 里还留着 weekly
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        library.cadence = "weekly"
        library.definition = {**(library.definition or {}), "cadence": "weekly"}
        await session.commit()

    async with get_sessionmaker()() as session:
        due = await ingest_service.find_due_daily_libraries(session)
        assert [str(lib.id) for lib in due] == [library_id]
        # 「下次自动同步」也不再因为 cadence 而显示为空
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        assert ingest_service.next_daily_sync_at(library) is not None


# ---- P9a：任务系统库化（VoyageRun 可挂方向库，独立库可直接触发抓取） ----


async def _promote_admin(email: str) -> None:
    """把已注册用户提为平台 admin（独立建库 / 库级 ingest 触发需要）。"""
    async with get_sessionmaker()() as session:
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one()
        user.role = "admin"
        await session.commit()


async def _create_standalone_library(client, headers, *, name="独立库-自动化科研", **extra):
    """经 POST /libraries 建一个不挂课题的独立库（project_id=NULL）。"""
    payload = {
        "name": name,
        "statement": DEFINITION["statement"],
        "rubric": DEFINITION["rubric"],
        "anchors": DEFINITION["anchor_papers"],
        "cadence": "daily",
        "keywords": DEFINITION["keywords"],
    }
    payload.update(extra)
    resp = await client.post("/api/libraries", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    library_id = body["id"]
    # P10：新库即刻是 active 个人库，无需审批即可触发抓取（token 记创建者账）。
    assert body["status"] == "active"
    return library_id


async def test_standalone_library_ingest_full_pipeline(client, queue_stub, wiki_mocks):
    """独立库（project_id=NULL）经 /libraries/{id}/ingest/run 触发并跑通全链路。

    校验：任务挂库（project_id 空、library_id 指向本库）、同库并发互斥、水位线写库、
    库版论文编译、库级用量归因、库级活动流（project_id 空 / library_id 指向本库）。
    """
    token = await register_and_login(client, email="lib-admin@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("lib-admin@example.com")
    library_id = await _create_standalone_library(client, headers)

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage = resp.json()
    assert voyage["kind"] == "wiki_bootstrap"
    assert voyage["library_id"] == library_id
    assert voyage["project_id"] is None  # 独立库无起源课题
    run_id = voyage["id"]
    assert ("run_voyage", (run_id,), {}) in queue_stub.jobs

    # 同库并发互斥 → 409（库化后互斥以库为准）
    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "INGEST_ALREADY_RUNNING"

    engine, _bus = _make_engine()
    await engine.run(uuid.UUID(run_id))

    detail = (await client.get(f"/api/voyages/{run_id}", headers=headers)).json()
    assert detail["status"] == "done", detail
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 6

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        # 水位线权威源写在库上
        assert library.ingest_state["watermark"]
        assert library.ingest_state["last_run"]["voyage_id"] == run_id

        # 库版论文：3 arXiv 候选，3 篇编译
        members = (
            (
                await session.execute(
                    select(LibraryPaper).where(LibraryPaper.library_id == library.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(members) == 3
        assert sum(1 for m in members if m.status == "compiled") == 2
        assert sum(1 for m in members if m.status == "excluded") == 1

        # 库级用量归因：ingest 全程 LLM 调用（打分/图注/编译/概念定义/向量化）记到库上
        usage = (
            (
                await session.execute(
                    select(LLMUsage).where(LLMUsage.library_id == library.id)
                )
            )
            .scalars()
            .all()
        )
        assert usage, "库级 ingest 应产生按库归因的用量记录"
        assert {u.library_id for u in usage} == {library.id}

        # 库级活动流：project_id 为空，library_id 指向本库
        acts = (
            (
                await session.execute(
                    select(Activity).where(Activity.library_id == library.id)
                )
            )
            .scalars()
            .all()
        )
        assert {"ingest.started", "ingest.completed"} <= {a.kind for a in acts}
        assert all(a.project_id is None for a in acts)


async def test_standalone_library_ingest_budget_gate(client, queue_stub):
    """独立库触发同样受库预算门约束：本月用尽 → 409 且不入队。"""
    token = await register_and_login(client, email="lib-budget@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("lib-budget@example.com")
    library_id = await _create_standalone_library(
        client, headers, name="独立库-预算", monthly_budget=1000
    )
    async with get_sessionmaker()() as session:
        session.add(
            LLMUsage(
                library_id=uuid.UUID(library_id),
                stage="librarian",
                model="fake",
                prompt_tokens=800,
                completion_tokens=300,  # 1100 ≥ 1000
            )
        )
        await session.commit()

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "bootstrap"},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "LIBRARY_BUDGET_EXHAUSTED"
    assert queue_stub.jobs == []


async def test_standalone_library_ingest_forbidden_for_stranger(client, queue_stub):
    """非管理者不能触发库级 ingest（成员/策展人/admin 之外 → 403）。"""
    token = await register_and_login(client, email="lib-owner2@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("lib-owner2@example.com")
    library_id = await _create_standalone_library(client, headers, name="独立库-权限")

    stranger = await register_and_login(client, email="lib-stranger@example.com")
    sh = {"Authorization": f"Bearer {stranger}"}
    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "bootstrap"},
        headers=sh,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "LIBRARY_MANAGE_FORBIDDEN"
    assert queue_stub.jobs == []


async def test_project_ingest_run_carries_library_id(client, queue_stub, wiki_mocks):
    """课题触发的 ingest 也只挂库：建库归实验室，不进课题的任务列表。"""
    project_id, headers = await _setup_project(client)
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage = resp.json()
    assert voyage["library_id"] is not None
    # 库任务不写 project_id —— 写了会混进课题的任务列表
    assert voyage["project_id"] is None

    async with get_sessionmaker()() as session:
        from app.services.libraries import get_library_for_project

        library = await get_library_for_project(session, uuid.UUID(project_id))
        assert voyage["library_id"] == str(library.id)
        run = await session.get(VoyageRun, uuid.UUID(voyage["id"]))
        assert run.library_id == library.id
        assert run.project_id is None


# ---- 每日 cron 的可达性与健壮性 ----


async def test_standalone_library_is_due_for_daily_cron(client, queue_stub, wiki_mocks):
    """独立库（project_id=NULL）也要进每日 cron。

    以前 cron 遍历的是 Project 再反查库，独立库一个都进不来——生产上 11 个活跃库里
    有 6 个是独立库，全靠人工点击才会同步。每日池只留 7 天，漏点就是永久漏抓。
    """
    token = await register_and_login(client, email="due-standalone@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("due-standalone@example.com")
    library_id = await _create_standalone_library(client, headers, name="独立库-cron")

    async with get_sessionmaker()() as session:
        assert await ingest_service.find_due_daily_libraries(session) == []  # 还没建库

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    async with get_sessionmaker()() as session:
        due = await ingest_service.find_due_daily_libraries(session)
        assert [str(lib.id) for lib in due] == [library_id]


async def test_non_active_library_is_not_due(client, queue_stub, wiki_mocks):
    """库 status 不是 active 就不该被 cron 拉起来——老的按课题选表压根没查这个字段。"""
    token = await register_and_login(client, email="due-inactive@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("due-inactive@example.com")
    library_id = await _create_standalone_library(client, headers, name="独立库-停用")

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        library.status = "rejected"
        await session.commit()
        assert await ingest_service.find_due_daily_libraries(session) == []


async def test_stale_paused_run_is_reclaimed_and_unblocks_the_library(
    client, queue_stub, wiki_mocks
):
    """卡了一天的 paused_error 由 cron 回收，该库随即恢复可同步。

    paused_error 不在 TERMINAL_STATUSES 里，互斥判定把它当「还在跑」，于是一次瞬时
    故障（arXiv 429、LLM 超时）就让这个库**永久**退出每日同步。生产上四个库正是如此。
    """
    import datetime as dt

    token = await register_and_login(client, email="stale-paused@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("stale-paused@example.com")
    library_id = await _create_standalone_library(client, headers, name="独立库-卡住")

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    # 造一个一天前就卡住的增量任务
    async with get_sessionmaker()() as session:
        stale = VoyageRun(
            kind="wiki_ingest",
            status="paused_error",
            library_id=uuid.UUID(library_id),
            goal="卡住的同步",
        )
        session.add(stale)
        await session.flush()
        stale.updated_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=48)
        await session.commit()
        stale_id = stale.id

    async with get_sessionmaker()() as session:
        assert await ingest_service.find_due_daily_libraries(session) == [], "卡住时不该被选中"

    async with get_sessionmaker()() as session:
        reclaimed = await ingest_service.reclaim_stale_paused_ingests(session)
        assert reclaimed == [stale_id]

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, stale_id)
        assert run.status == "cancelled"
        due = await ingest_service.find_due_daily_libraries(session)
        assert [str(lib.id) for lib in due] == [library_id], "回收后该库应恢复可同步"


async def test_recent_paused_run_is_left_alone(client, queue_stub, wiki_mocks):
    """刚失败的任务不回收——留一天窗口给人手动 resume。"""
    token = await register_and_login(client, email="fresh-paused@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("fresh-paused@example.com")
    library_id = await _create_standalone_library(client, headers, name="独立库-刚卡住")

    async with get_sessionmaker()() as session:
        fresh = VoyageRun(
            kind="wiki_ingest",
            status="paused_error",
            library_id=uuid.UUID(library_id),
            goal="刚失败",
        )
        session.add(fresh)
        await session.commit()

    async with get_sessionmaker()() as session:
        assert await ingest_service.reclaim_stale_paused_ingests(session) == []


async def test_over_budget_library_does_not_stop_the_others(client, queue_stub, wiki_mocks):
    """一个库超预算不能拖垮当天其余的库。

    老写法只捕获 IngestConflictError，LibraryBudgetExhaustedError 会穿透整个循环，
    排在后面的库当天全都不同步，而且只在 arq 日志里留个异常，界面上完全无感。
    """
    from worker.tasks import daily_wiki_ingest

    token = await register_and_login(client, email="budget-cron@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("budget-cron@example.com")

    lib_ids = []
    for name in ("独立库-超预算", "独立库-正常"):
        lib_id = await _create_standalone_library(client, headers, name=name)
        resp = await client.post(
            f"/api/libraries/{lib_id}/ingest/run",
            json={"mode": "bootstrap", "knobs": KNOBS},
            headers=headers,
        )
        engine, _ = _make_engine()
        await engine.run(uuid.UUID(resp.json()["id"]))
        lib_ids.append(lib_id)
    broke_id, healthy_id = lib_ids

    # 第一个库月度预算压到 1 token —— bootstrap 已经用掉不止这些，必然耗尽
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(broke_id))
        library.monthly_budget = 1
        await session.commit()

    class _Redis:
        def __init__(self) -> None:
            self.jobs: list[tuple] = []

        async def enqueue_job(self, name, *args, **kwargs):
            self.jobs.append((name, args))

    redis = _Redis()
    enqueued = await daily_wiki_ingest({"redis": redis})

    # 超预算的那个被跳过，正常的那个照常入队
    async with get_sessionmaker()() as session:
        runs = {
            str(r.library_id): r
            for r in (
                await session.execute(
                    select(VoyageRun).where(VoyageRun.id.in_([uuid.UUID(i) for i in enqueued]))
                )
            )
            .scalars()
            .all()
        }
    assert healthy_id in runs, "正常的库必须仍被入队"
    assert broke_id not in runs
    assert len(redis.jobs) == len(enqueued) == 1


async def test_truncated_run_does_not_advance_the_watermark(client, queue_stub, wiki_mocks):
    """被预算截断的运行不推进水位线。

    预算是在**步骤之间**检查的：打分跑完把预算烧光，引擎就把剩余步骤置 obsolete，
    再拿最后一个待办步骤当隐式收尾——而本流水线的最后一步正是 update_watermark。
    推进水位线等于「一篇没处理，却宣布这段时间已经看过了」，下次同步直接跳过这批论文。
    """
    from app.agents.voyage.actions import ActionContext
    from app.agents.voyage.actions_wiki import update_watermark
    from app.models.voyage import VoyageStep

    token = await register_and_login(client, email="truncated@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("truncated@example.com")
    library_id = await _create_standalone_library(client, headers, name="独立库-截断")

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        original = library.ingest_state["watermark"]
        assert original

        # 造一个被预算截断的增量运行：有步骤被置 obsolete
        run = VoyageRun(
            kind="wiki_ingest",
            status="executing",
            library_id=uuid.UUID(library_id),
            goal="截断的同步",
        )
        session.add(run)
        await session.flush()
        session.add(
            VoyageStep(
                run_id=run.id, action="wiki.compile", title="编译", status="obsolete", seq=0
            )
        )
        await session.commit()
        run_id = run.id

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        ctx = ActionContext(
            run=run,
            llm=LLMRouter(),
            checkpoint={"watermark_candidate": "2099-01-01T00:00:00+00:00"},
        )
        result = await update_watermark(ctx, {})

    assert result["truncated"] is True
    assert result["watermark"] == original, "截断的运行不该推进水位线"

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        assert library.ingest_state["watermark"] == original


# ---- 三种收集模式 ----


def test_search_query_puts_exclusions_behind_andnot():
    from app.services.literature.arxiv import build_search_query

    q = build_search_query(["cs.AI"], ["agent"], exclude=["speech recognition"])
    assert q.startswith('(cat:cs.AI) AND (all:"agent")')
    assert 'ANDNOT all:"speech recognition"' in q
    # 没有排除词就不该多出尾巴
    assert "ANDNOT" not in build_search_query(["cs.AI"], ["agent"])


async def test_snowball_mode_skips_the_arxiv_search(client, queue_stub, wiki_mocks):
    """锚点扩展模式只走引文扩展，不检索 arXiv——想补一轮引文不必连带再搜一次。"""
    token = await register_and_login(client, email="mode-snowball@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("mode-snowball@example.com")
    library_id = await _create_standalone_library(client, headers, name="独立库-锚点")

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "snowball", "knobs": KNOBS},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    detail = (await client.get(f"/api/voyages/{resp.json()['id']}", headers=headers)).json()
    assert detail["status"] == "done", detail
    assert detail["steps"][0]["action"] == "wiki.snowball"
    assert "锚点" in detail["steps"][0]["title"]
    assert [s["action"] for s in detail["steps"]] == [
        "wiki.snowball",
        "wiki.score_relevance",
        "wiki.fetch_extract",
        "wiki.compile",
        "wiki.link_concepts",
        "wiki.update_watermark",
    ]


async def test_search_mode_uses_given_terms_and_time_range(client, queue_stub, wiki_mocks):
    """检索模式：本次指定的查询词与时间范围要真的进检索式。"""
    token = await register_and_login(client, email="mode-search@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("mode-search@example.com")
    library_id = await _create_standalone_library(client, headers, name="独立库-检索")

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={
            "mode": "search",
            "knobs": KNOBS,
            "query_terms": ["world model"],
            "time_range": "1w",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    detail = (await client.get(f"/api/voyages/{resp.json()['id']}", headers=headers)).json()
    assert detail["status"] == "done", detail
    assert detail["steps"][0]["action"] == "wiki.search_candidates"

    calls = [
        str(c.request.url)
        for c in wiki_mocks.calls
        if "export.arxiv.org" in str(c.request.url)
    ]
    assert calls, "检索模式必须真的打了 arXiv 检索接口"
    assert "world+model" in calls[0] or "world%20model" in calls[0]
    # 一周窗口：起始日期应当离今天很近（而不是默认的半年）
    import re as _re

    lo = _re.search(r"submittedDate%3A%5B(\d{8})", calls[0])
    assert lo, calls[0]
    from datetime import UTC, datetime, timedelta

    since = datetime.strptime(lo.group(1), "%Y%m%d").replace(tzinfo=UTC)
    assert datetime.now(UTC) - since < timedelta(days=10)


async def test_legacy_bootstrap_mode_still_works(client, queue_stub, wiki_mocks):
    """存量调用传的 bootstrap 折算成 search，不报错、不改变步骤形状。"""
    project_id, headers = await _setup_project(client)
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))
    detail = (await client.get(f"/api/voyages/{resp.json()['id']}", headers=headers)).json()
    assert detail["status"] == "done", detail
    assert detail["steps"][0]["action"] == "wiki.search_candidates"


async def test_exclude_terms_filter_the_daily_feed_sync(client, queue_stub, wiki_mocks):
    """排除关键词在每日池同步时**硬过滤**，并把滤掉的篇数报进观测。

    与「包括关键词」区别对待：include 配窄了会让库悄无声息颗粒无收，所以同步时不拿它
    筛；exclude 是用户明确说「我不要这个」，漏掉它是失职。
    """
    import datetime as dt

    from app.models.daily_feed import DailyFeedEntry

    token = await register_and_login(client, email="excl@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("excl@example.com")
    library_id = await _create_standalone_library(client, headers, name="独立库-排除词")

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "search", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        definition = dict(library.definition or {})
        definition["keywords"] = {
            **(definition.get("keywords") or {}),
            "exclude": ["speech recognition"],
        }
        library.definition = definition
        for i, title in enumerate(["Planning Agents", "Speech Recognition at Scale"]):
            paper = new_paper(
                source="arxiv",
                arxiv_id=f"2607.7000{i}",
                title=title,
                abstract="research",
                year=2026,
                published_at=dt.datetime(2026, 7, 20, tzinfo=dt.UTC),
            )
            session.add(paper)
            await session.flush()
            session.add(
                DailyFeedEntry(
                    paper_id=paper.id, feed_date=dt.date(2026, 7, 20), primary_category="cs.AI"
                )
            )
        await session.commit()

    resp2 = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "incremental", "knobs": KNOBS},
        headers=headers,
    )
    engine2, _ = _make_engine()
    await engine2.run(uuid.UUID(resp2.json()["id"]))

    detail = (await client.get(f"/api/voyages/{resp2.json()['id']}", headers=headers)).json()
    obs = detail["steps"][0]["observation"]
    assert obs["feed_total"] == 2
    assert obs["excluded_by_terms"] == 1  # 命中排除词的那篇被挡下，且报了出来
    assert obs["inserted"] == 1

    async with get_sessionmaker()() as session:
        titles = set(
            (
                await session.execute(
                    select(Paper.title)
                    .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                    .where(LibraryPaper.library_id == uuid.UUID(library_id))
                )
            )
            .scalars()
            .all()
        )
    assert "Speech Recognition at Scale" not in titles
