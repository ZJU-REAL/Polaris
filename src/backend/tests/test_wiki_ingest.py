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
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.llm_config import LLMUsage
from app.models.paper import EMBEDDING_DIM, paper_concepts
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
)

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
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 7
    obs0 = detail["steps"][0]["observation"]
    assert obs0["found"] == 3 and obs0["inserted"] == 3
    assert detail["steps"][1]["observation"]["inserted"] == 1  # 雪球 1 篇

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        assert len(rows) == 4  # 3 arXiv 候选 + 1 雪球
        by_status = {}
        for p, m in rows:
            by_status.setdefault(m.status, []).append((p, m))
        assert len(by_status.get("excluded", [])) == 1  # "irrelevant" 论文被排除
        assert by_status["excluded"][0][0].arxiv_id == "2406.00003"
        compiled_rows = by_status.get("compiled", [])
        assert len(compiled_rows) == 3
        for p, m in compiled_rows:
            assert m.relevance_score is not None and m.relevance_score >= 0.6
            assert m.scored_at is not None and m.compiled_at is not None
            assert m.compiled_model == "fake-default"  # 编译所用模型落库（voyage 路径）
            assert p.tldr
            assert "[[Agent]]" in m.wiki_content  # 双链
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

        concepts = await project_concepts(session, project_id=project_id)
        names = {c.name for c in concepts}
        assert names == {"Agent", "强化学习"}
        for c in concepts:
            assert c.definition and c.slug
            assert c.category == "method"
        links = int(
            (await session.execute(select(func.count()).select_from(paper_concepts))).scalar_one()
        )
        assert links == 6  # 3 篇编译论文 × 2 概念

        # P8a：水位线权威源在库（library.ingest_state），不再写起源课题
        from app.services.libraries import get_library_for_project

        library = await get_library_for_project(session, uuid.UUID(project_id))
        assert library.ingest_state["watermark"]
        assert library.ingest_state["last_run"]["voyage_id"] == run_id

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
    # 增量回看窗口 = 水位线 − 14 天（覆盖 arXiv 关键词索引滞后，防漏抓近几天新论文）
    from datetime import datetime, timedelta

    watermark_dt = datetime.fromisoformat(state["watermark"])
    since_dt = datetime.fromisoformat(detail2["steps"][0]["observation"]["window_since"])
    assert watermark_dt - since_dt == timedelta(days=14)
    async with get_sessionmaker()() as session:
        assert len(await project_paper_rows(session, project_id=project_id)) == 4


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
        rows = await project_paper_rows(session, project_id=project_id)
        scored = sum(1 for _, m in rows if m.status in ("scored", "excluded"))
        assert scored == 3  # 逐篇 commit：崩溃前的进度已落库

    # resume：从断点续跑到 done，总打分调用数 == 论文数（无重复）
    engine2, _ = _make_engine()
    await engine2.resume(run_id)
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    assert resp.json()["status"] == "done"
    assert await _relevance_call_count() == 4  # 4 篇论文各打分一次

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        assert sorted(m.status for _, m in rows) == [
            "compiled",
            "compiled",
            "compiled",
            "excluded",
        ]


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
        for _, m in await project_paper_rows(session, project_id=project_id):
            by_status[m.status] = by_status.get(m.status, 0) + 1
        # 失败打分的那篇仍是 candidate（下次续跑会重试），其余照常推进
        assert by_status.get("candidate", 0) == 1
        assert by_status.get("excluded", 0) == 1  # irrelevant 篮子编织论文
        assert by_status.get("compiled", 0) == 2  # 两篇通过阈值的论文成功编译


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
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 7
    assert detail["steps"][0]["observation"]["inserted"] == 3  # 默认分类兜底后仍能检索

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        # 无锚点论文 → 雪球 0 篇；3 候选：2 编译 + 1 排除（无 rubric 时打分只用 statement）
        assert sorted(m.status for _, m in rows) == ["compiled", "compiled", "excluded"]


def test_rss_loose_keyword_filter():
    """宽松关键词过滤：连字符变体命中、无关滤除、无关键词全留（确定性单元测试）。"""
    from app.agents.voyage.actions_wiki import _keyword_match, _normalize_kw

    includes = [_normalize_kw(k) for k in ["Computer Use Agent"]]
    hit = {"title": "Computer-Use Agents at Scale", "abstract": "results"}
    miss = {"title": "Basket Weaving", "abstract": "nothing relevant"}
    assert _keyword_match(hit, includes) is True  # 连字符 vs 空格变体命中
    assert _keyword_match(miss, includes) is False  # 无关滤除
    assert _keyword_match(miss, []) is True  # 无关键词 → 全留给 LLM 打分


async def test_incremental_rss_fresh_layer(client, queue_stub, wiki_mocks):
    """增量同步的 RSS 新鲜源：announce_type 过滤、版本号归一化、宽松关键词过滤、三方去重。"""
    project_id, headers = await _setup_project(client)

    # 先 bootstrap 建立水位线与存量论文（2406.00001 等入库）
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    # 仅本测试给 wiki_mocks 路由追加 RSS 新鲜源（bootstrap 不走 RSS，故此前无需）
    wiki_mocks.get(url__regex=r"https://rss\.arxiv\.org/rss/.*").mock(
        return_value=httpx.Response(200, text=ARXIV_RSS)
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
    assert obs["mode"] == "incremental"
    # API 窗口检索的 3 篇全去重（已在 bootstrap 入库）→ 新增全部来自 RSS
    assert obs["rss_found"] == 4  # 6 条 item 中 4 条 new/cross（replace/replace-cross 跳过）
    assert obs["rss_matched"] == 3  # 宽松关键词过滤：篮子编织被滤除
    assert obs["rss_inserted"] == 2  # 3 命中里 2406.00001 与存量重复被去重
    assert obs["inserted"] == 2  # 本步入库总数 = RSS 新增
    assert "cs.LG" in obs["rss_categories"]

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        by_aid = {p.arxiv_id: m.status for p, m in rows}
        # 版本号已归一化（无 v1/v2 后缀），新鲜论文入库
        assert "2607.30001" in by_aid
        assert "2607.30002" in by_aid
        # replace / replace-cross（旧论文更新）与无关论文未入库
        assert "2607.30003" not in by_aid
        assert "2607.30004" not in by_aid
        assert "2607.30005" not in by_aid
        # 与存量重复的 arxiv_id 未产生第二条记录
        dup_count = sum(1 for p, _ in rows if p.arxiv_id == "2406.00001")
        assert dup_count == 1


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
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 7

    # 检索：分页抓到全部 210 条（旧逻辑 limit=min(200, 10*3)=30）
    obs0 = detail["steps"][0]["observation"]
    assert obs0["found"] == _BIG_N and obs0["inserted"] == _BIG_N
    # 雪球：锚点扩展 1 篇（上限放开后不受 max_papers*2 影响）
    assert detail["steps"][1]["observation"]["inserted"] == 1
    total = _BIG_N + 1
    # 打分：211 篇全部处理（旧逻辑截 200）
    score_obs = detail["steps"][2]["observation"]
    assert score_obs["processed"] == total and score_obs["succeeded"] == total
    assert score_obs["excluded"] == 0  # 全部相关（fake 打 0.88 ≥ 0.6）
    # 抽取/编译：211 篇全部进入（旧逻辑截 min(compile_top_n=5, max_papers=10)=5）
    assert detail["steps"][3]["observation"]["processed"] == total
    compile_obs = detail["steps"][4]["observation"]
    assert compile_obs["processed"] == total and compile_obs["succeeded"] == total

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        compiled = sum(1 for _, m in rows if m.status == "compiled")
        assert compiled == total  # 全部编译落库，无一截断


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
    library_id = resp.json()["id"]
    # P9b：新库 pending，需 admin 审批激活后才能触发抓取（调用方均已 promote_admin）。
    resp = await client.post(f"/api/libraries/{library_id}/approve", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"
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
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 7

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        # 水位线权威源写在库上
        assert library.ingest_state["watermark"]
        assert library.ingest_state["last_run"]["voyage_id"] == run_id

        # 库版论文：3 arXiv 候选 + 1 雪球，3 篇编译
        members = (
            (
                await session.execute(
                    select(LibraryPaper).where(LibraryPaper.library_id == library.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(members) == 4
        assert sum(1 for m in members if m.status == "compiled") == 3
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
    """隐式库不回归：课题触发的 ingest 现在既带 project_id 也带 library_id（同库解析一致）。"""
    project_id, headers = await _setup_project(client)
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage = resp.json()
    assert voyage["project_id"] == project_id
    assert voyage["library_id"] is not None

    async with get_sessionmaker()() as session:
        from app.services.libraries import get_library_for_project

        library = await get_library_for_project(session, uuid.UUID(project_id))
        assert voyage["library_id"] == str(library.id)
        run = await session.get(VoyageRun, uuid.UUID(voyage["id"]))
        assert run.library_id == library.id
        assert run.project_id == uuid.UUID(project_id)
