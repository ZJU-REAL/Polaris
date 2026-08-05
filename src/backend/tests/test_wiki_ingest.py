"""wiki ingest 全流程测试：respx mock 文献 API + fake LLM，直接驱动 VoyageEngine。

覆盖：检索模式全链路（检索→打分→全文→编译→概念→记录进度）、锚点扩展模式、
并发 409、断点恢复不重复打分（fake LLM 调用计数）、增量续跑、每日 cron 选表。
"""

import asyncio
import uuid
from datetime import timedelta

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
from app.models.research_digest import LibraryResearchDigest
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


def _today():
    """每日池条目一律用今天：库同步默认只看「上次同步以来」的窗口。"""
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).date()


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
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 8
    obs0 = detail["steps"][0]["observation"]
    assert obs0["found"] == 3 and obs0["inserted"] == 3

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        # 3 篇候选里那篇 "irrelevant" 相关性不达标，成员行被直接删掉（不进回收站），
        # 所以库里只剩 2 行
        assert len(rows) == 2
        by_status = {}
        for p, m in rows:
            by_status.setdefault(m.status, []).append((p, m))
        assert not by_status.get("excluded")
        assert "2406.00003" not in {p.arxiv_id for p, _ in rows}
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
                await session.execute(select(Activity).where(Activity.library_id == library.id))
            ).scalars()
        }
        assert {"ingest.started", "ingest.completed"} <= activity_kinds
        digest = (
            await session.execute(
                select(LibraryResearchDigest).where(
                    LibraryResearchDigest.voyage_id == uuid.UUID(run_id)
                )
            )
        ).scalar_one()
        assert digest.counts["kept"] == 2
        assert digest.counts["excluded"] == 1
        assert len(digest.paper_insights) == 2
        assert all(item["concepts"] == ["Agent", "强化学习"] for item in digest.paper_insights)
        assert "**发表时间**：" in digest.content
        assert "**概念**：[[Agent]] · [[强化学习]]" in digest.content
        assert "## 排除清单（1 篇）" in digest.content
        assert digest.rolling_trends
        library_id = str(library.id)

    resp = await client.get(f"/api/libraries/{library_id}/digests", headers=headers)
    assert resp.status_code == 200, resp.text
    summaries = resp.json()
    assert summaries[0]["counts"]["kept"] == 2
    resp = await client.get(
        f"/api/libraries/{library_id}/digests/{summaries[0]['id']}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["trend_content"].startswith("# 滚动趋势")

    # ingest/state：完成后的水位线与计数
    resp = await client.get(f"/api/projects/{project_id}/ingest/state", headers=headers)
    state = resp.json()
    assert state["watermark"]
    assert state["running_voyage_id"] is None
    assert state["last_run"]["voyage_id"] == run_id
    assert state["last_run"]["status"] == "done"
    counts = state["paper_counts"]
    # 淘汰的那篇已不在库内，所以 total 是 2 而不是 3
    assert counts["compiled"] == 2 and counts["excluded"] == 0 and counts["total"] == 2

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
        # 仍是 2：增量这轮没有新候选，上一轮淘汰的那篇也不会复活
        assert len(await project_paper_rows(session, project_id=project_id)) == 2


class _BreakDailyDigest(FakeProvider):
    """让每日简报产出非法 JSON，验证水位线不会越过失败产物。"""

    async def complete(
        self,
        messages,
        *,
        model,
        temperature=0.7,
        max_tokens=None,
        images=None,
        effort=None,
    ):
        if any("POLARIS_DAILY_DIGEST" in message.content for message in messages):
            from app.core.llm.base import CompletionResult

            return CompletionResult(content="(empty report)", model=model, finish_reason="stop")
        return await super().complete(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            images=images,
            effort=effort,
        )


async def test_backwards_clock_keeps_scored_papers_in_the_run(
    client, queue_stub, wiki_mocks, monkeypatch
):
    """墙钟回拨也不能把刚打完分的论文丢出本次同步（#234）。

    取全文/编译/简报要认出「本次收下的那批论文」。曾按 ``scored_at >= run.created_at``
    划时间窗口：两个时间戳分别取自建任务和打分两个时刻的系统墙钟，NTP 回拨、或 API 与
    worker 不在一台机器上，都会让刚打完分的论文落在窗口外被静默排除——生产上「已打分
    却没全文」的积压就是这么攒出来的。这里把打分时刻整体拨到建任务之前来钉住这个语义。
    """
    import app.services.relevance as relevance_module

    real_utcnow = relevance_module.utcnow
    monkeypatch.setattr(relevance_module, "utcnow", lambda: real_utcnow() - timedelta(seconds=30))

    project_id, headers = await _setup_project(client)
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    run_id = resp.json()["id"]
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(run_id))

    detail = (await client.get(f"/api/voyages/{run_id}", headers=headers)).json()
    assert detail["status"] == "done", detail
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 8
    # 3 篇候选里 1 篇不相关被淘汰，剩下 2 篇必须一路走到编译，不因时钟回拨掉队
    assert detail["steps"][2]["observation"]["processed"] == 2  # 取全文
    assert detail["steps"][3]["observation"]["processed"] == 2  # 编译

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        assert [m.status for _, m in rows] == ["compiled"] * 2
        # 打分时刻确实早于建任务（回拨生效），归属仍靠运行 id 认出来
        run = await session.get(VoyageRun, uuid.UUID(run_id))
        for _, m in rows:
            assert m.scored_at < run.created_at
            assert m.scored_run_id == run.id


async def test_daily_digest_failure_does_not_advance_watermark(client, queue_stub, wiki_mocks):
    """成功跑完前置步骤但没有有效简报时视为空跑，趋势与 watermark 均不得推进。"""
    project_id, headers = await _setup_project(client)
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    run_id = uuid.UUID(resp.json()["id"])

    router = LLMRouter()
    router.override_provider(_BreakDailyDigest())
    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=router)
    await engine.run(run_id)

    detail = (await client.get(f"/api/voyages/{run_id}", headers=headers)).json()
    assert detail["status"] == "paused_error", detail
    assert [step["status"] for step in detail["steps"][:5]] == ["passed"] * 5
    assert detail["steps"][5]["status"] == "failed"
    assert detail["steps"][5]["observation"]["error"].startswith("ValueError")
    assert [step["status"] for step in detail["steps"][6:]] == ["pending", "pending"]

    async with get_sessionmaker()() as session:
        from app.services.libraries import get_library_for_project

        library = await get_library_for_project(session, uuid.UUID(project_id))
        assert not (library.ingest_state or {}).get("watermark")
        digest = (
            await session.execute(
                select(LibraryResearchDigest).where(LibraryResearchDigest.voyage_id == run_id)
            )
        ).scalar_one_or_none()
        assert digest is None


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
    crashing_router.override_provider(_CrashOnNthRelevance(crash_at=2))
    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=crashing_router)
    with pytest.raises(asyncio.CancelledError):
        await engine.run(run_id)

    assert await _relevance_call_count() == 2  # 崩溃前 2 篇成功打分（并发，非串行的 1）
    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        # 逐篇 commit：崩溃前的进度已落库。达标的留下成员行，不达标的整行删掉，
        # 所以「已处理」= 剩下的非 candidate 行 + 消失的行
        scored = sum(1 for _, m in rows if m.status != "candidate") + (3 - len(rows))
        assert scored == 2

    # resume：从断点续跑到 done，总打分调用数 == 论文数（无重复）
    engine2, _ = _make_engine()
    await engine2.resume(run_id)
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    assert resp.json()["status"] == "done"
    assert await _relevance_call_count() == 3  # 3 篇论文各打分一次

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        assert sorted(m.status for _, m in rows) == ["compiled", "compiled"]


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
    router.override_provider(_BreakOneRelevance(break_marker="Benchmark"))
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
        assert by_status.get("excluded", 0) == 0  # irrelevant 那篇整行删掉，不进回收站
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
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 8
    assert detail["steps"][0]["observation"]["inserted"] == 3  # 默认分类兜底后仍能检索

    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        # 3 候选：2 编译 + 1 淘汰（淘汰的整行删掉；无 rubric 时打分只用 statement）
        assert sorted(m.status for _, m in rows) == ["compiled", "compiled"]


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
                published_at=dt.datetime.now(dt.UTC),
            )
            session.add(paper)
            await session.flush()
            feed_ids.append(paper.id)
            # 用今天：库同步默认只看「上次同步以来」的窗口，固定的过去日期会被筛掉
            session.add(
                DailyFeedEntry(paper_id=paper.id, feed_date=_today(), primary_category="cs.AI")
            )
        # 已在库的论文也进池：必须被去重挡住，不能重复送打分
        session.add(
            DailyFeedEntry(paper_id=existing_id, feed_date=_today(), primary_category="cs.AI")
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
    # 预算按「最大检索篇数」派生（上限而非开销；真正花钱的是编译那部分）
    assert ingest_service.derive_budget(IngestKnobs()) == {"max_tokens": 150 * 20_000}

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
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 8

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
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
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
    assert [s["status"] for s in detail["steps"]] == ["passed"] * 8

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
        # 淘汰的那篇成员行被删掉，库里只剩 2 行
        assert len(members) == 2
        assert sum(1 for m in members if m.status == "compiled") == 2
        assert sum(1 for m in members if m.status == "excluded") == 0

        # 库级用量归因：ingest 全程 LLM 调用（打分/图注/编译/概念定义/向量化）记到库上
        usage = (
            (await session.execute(select(LLMUsage).where(LLMUsage.library_id == library.id)))
            .scalars()
            .all()
        )
        assert usage, "库级 ingest 应产生按库归因的用量记录"
        assert {u.library_id for u in usage} == {library.id}

        # 库级活动流：project_id 为空，library_id 指向本库
        acts = (
            (await session.execute(select(Activity).where(Activity.library_id == library.id)))
            .scalars()
            .all()
        )
        assert {"ingest.started", "ingest.completed"} <= {a.kind for a in acts}
        assert all(a.project_id is None for a in acts)


async def test_manual_digest_reuses_today_updates_without_incremental_ingest(
    client, queue_stub, wiki_mocks
):
    """今日已有评分更新时，手动简报只跑简报/趋势，不重复检索且不推进水位线。"""
    token = await register_and_login(client, email="lib-digest@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    library_id = await _create_standalone_library(client, headers, name="独立库-今日简报")

    bootstrap = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    assert bootstrap.status_code == 201, bootstrap.text
    bootstrap_id = bootstrap.json()["id"]
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(bootstrap_id))
    state_before = (
        await client.get(f"/api/libraries/{library_id}/ingest/state", headers=headers)
    ).json()

    resp = await client.post(f"/api/libraries/{library_id}/digests/generate", headers=headers)
    assert resp.status_code == 201, resp.text
    launch = resp.json()
    assert launch["strategy"] == "digest_only"
    assert launch["paper_count"] == 2
    digest_run_id = launch["voyage_id"]
    assert ("run_voyage", (digest_run_id,), {}) in queue_stub.jobs

    await engine.run(uuid.UUID(digest_run_id))
    detail = (await client.get(f"/api/voyages/{digest_run_id}", headers=headers)).json()
    assert detail["status"] == "done", detail
    assert [step["action"] for step in detail["steps"]] == [
        "wiki.daily_digest",
        "wiki.trend_synthesize",
    ]

    state_after = (
        await client.get(f"/api/libraries/{library_id}/ingest/state", headers=headers)
    ).json()
    assert state_after["watermark"] == state_before["watermark"]
    assert state_after["last_run"] == state_before["last_run"]
    async with get_sessionmaker()() as session:
        digest = (
            await session.execute(
                select(LibraryResearchDigest).where(
                    LibraryResearchDigest.voyage_id == uuid.UUID(digest_run_id)
                )
            )
        ).scalar_one()
        assert digest.counts["kept"] == 2
        assert digest.counts["excluded"] == 1
        assert any("跳过增量抓取" in message for message in digest.source_diagnostics["messages"])


async def test_manual_digest_falls_back_to_incremental_when_no_today_updates(client, queue_stub):
    """今日没有评分更新时，手动简报入口退回完整增量任务。"""
    token = await register_and_login(client, email="lib-digest-empty@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    library_id = await _create_standalone_library(client, headers, name="独立库-无今日更新")

    resp = await client.post(f"/api/libraries/{library_id}/digests/generate", headers=headers)
    assert resp.status_code == 201, resp.text
    launch = resp.json()
    assert launch["strategy"] == "incremental"
    assert launch["paper_count"] == 0
    assert ("run_voyage", (launch["voyage_id"],), {}) in queue_stub.jobs
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, uuid.UUID(launch["voyage_id"]))
        assert run.kind == "wiki_ingest"
        assert run.checkpoint["params"].get("digest_only") is None


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


async def test_one_manual_sync_does_not_cancel_everyone_elses_daily_sync(
    client, queue_stub, wiki_mocks
):
    """某个库今天被手动同步过，不影响其余库当天的自动同步。

    生产上 07-31 就是这样丢了一整轮：10:22 有人手动同步了 rubric 一个库，10:32 每日池
    刚灌进 227 篇新论文、扇出被触发，结果一个库都没选中——因为闸门是
    ``already_ran_today("wiki_ingest")``，查的是**当天任意一条** wiki_ingest 运行，
    不分库、也不分手动还是自动。
    """
    token = await register_and_login(client, email="fanout-manual@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("fanout-manual@example.com")

    first = await _create_standalone_library(client, headers, name="被手动同步的库")
    second = await _create_standalone_library(client, headers, name="等自动同步的库")
    engine, _ = _make_engine()
    for library_id in (first, second):
        resp = await client.post(
            f"/api/libraries/{library_id}/ingest/run",
            json={"mode": "bootstrap", "knobs": KNOBS},
            headers=headers,
        )
        await engine.run(uuid.UUID(resp.json()["id"]))

    async with get_sessionmaker()() as session:
        # 有人手动同步了第一个库（created_by 非空，且已跑完）
        me = (
            await session.execute(
                select(User).where(User.email == "fanout-manual@example.com")
            )
        ).scalar_one()
        session.add(
            VoyageRun(
                kind="wiki_ingest",
                goal="手动同步",
                status="done",
                library_id=uuid.UUID(first),
                created_by=me.id,
            )
        )
        await session.commit()

    # 走真正的扇出任务：闸门以前就在这里，直接调选库函数是测不到的
    from worker.tasks import daily_wiki_ingest

    enqueued: list[str] = []

    class _Redis:
        async def enqueue_job(self, name, run_id, **kwargs):
            enqueued.append(run_id)

    created = await daily_wiki_ingest({"redis": _Redis()})
    assert enqueued == created

    async with get_sessionmaker()() as session:
        synced = {
            str((await session.get(VoyageRun, uuid.UUID(rid))).library_id) for rid in created
        }
    assert second in synced, "别人手动同步了一个库，不该让这个库当天不再自动同步"
    assert first in synced, "手动同步跑在池子更新之前，替代不了这一轮"


async def test_a_library_is_only_auto_synced_once_a_day(client, queue_stub, wiki_mocks):
    """同一个库当天已经**自动**同步过就不再选——「每天一轮」的语义按库保留。"""
    token = await register_and_login(client, email="fanout-once@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("fanout-once@example.com")
    library_id = await _create_standalone_library(client, headers, name="每天一轮的库")

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    async with get_sessionmaker()() as session:
        assert [str(lib.id) for lib in await ingest_service.find_due_daily_libraries(session)] == [
            library_id
        ]
        session.add(
            VoyageRun(
                kind="wiki_ingest",
                goal="自动同步",
                status="done",
                library_id=uuid.UUID(library_id),
                created_by=None,  # 自动
            )
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        assert await ingest_service.find_due_daily_libraries(session) == []


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
            VoyageStep(run_id=run.id, action="wiki.compile", title="编译", status="obsolete", seq=0)
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
        "wiki.daily_digest",
        "wiki.trend_synthesize",
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
        str(c.request.url) for c in wiki_mocks.calls if "export.arxiv.org" in str(c.request.url)
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
                published_at=dt.datetime.now(dt.UTC),
            )
            session.add(paper)
            await session.flush()
            session.add(
                DailyFeedEntry(paper_id=paper.id, feed_date=_today(), primary_category="cs.AI")
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


async def test_sync_scope_defaults_to_today_only(client, queue_stub, wiki_mocks):
    """库同步默认只扫当天新入池的论文；管理员可切到扫全池。

    同步由每日抓取跑完后触发，新论文本就只有那一批。扫全池的代价是重复排序几千篇
    早就处理过的；它的价值在自愈——上次失败落下的还能补回来，所以留作可选。
    """
    import datetime as dt

    from app.models.daily_feed import DailyFeedEntry
    from app.services import daily_feed

    token = await register_and_login(client, email="scope@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("scope@example.com")
    library_id = await _create_standalone_library(client, headers, name="独立库-范围")

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "search", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    today = dt.datetime.now(dt.UTC).date()
    async with get_sessionmaker()() as session:
        for i, day in enumerate((today, today - dt.timedelta(days=3))):
            paper = new_paper(
                source="arxiv", arxiv_id=f"2607.8100{i}", title=f"Feed {i}", abstract="research"
            )
            session.add(paper)
            await session.flush()
            session.add(DailyFeedEntry(paper_id=paper.id, feed_date=day, primary_category="cs.AI"))
        await session.commit()

    async def run_sync() -> dict:
        r = await client.post(
            f"/api/libraries/{library_id}/ingest/run",
            json={"mode": "incremental", "knobs": KNOBS},
            headers=headers,
        )
        eng, _ = _make_engine()
        await eng.run(uuid.UUID(r.json()["id"]))
        detail = (await client.get(f"/api/voyages/{r.json()['id']}", headers=headers)).json()
        return detail["steps"][0]["observation"]

    # 默认 since_last：上次同步是刚才那次建库，所以窗口从今天起 → 只看当天那条
    obs = await run_sync()
    assert obs["feed_scope"] == "since_last"
    assert obs["feed_total"] == 1

    async with get_sessionmaker()() as session:
        await daily_feed.set_sync_scope(session, "daily")
    obs = await run_sync()
    assert obs["feed_scope"] == "daily"
    assert obs["feed_total"] == 1, "只看当天那条"

    async with get_sessionmaker()() as session:
        await daily_feed.set_sync_scope(session, "full")
    obs = await run_sync()
    assert obs["feed_scope"] == "full"
    assert obs["feed_total"] == 2, "切到 full 后旧的那条也进扫描范围"


async def test_since_last_scope_widens_after_a_missed_sync(client, queue_stub, wiki_mocks):
    """since_last 的价值:漏了几天会自动把那几天补扫回来。

    daily 模式下漏掉就永久错过(arXiv 不会再给第二次)；full 模式每次重复排序几千篇。
    since_last 正常时和 daily 一样省，出事时和 full 一样能自愈。
    """
    import datetime as dt

    from app.models.daily_feed import DailyFeedEntry
    from app.services import daily_feed

    token = await register_and_login(client, email="sincelast@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _promote_admin("sincelast@example.com")
    library_id = await _create_standalone_library(client, headers, name="独立库-自愈")

    resp = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "search", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))

    today = dt.datetime.now(dt.UTC).date()
    async with get_sessionmaker()() as session:
        for i, day in enumerate((today, today - dt.timedelta(days=2))):
            paper = new_paper(
                source="arxiv", arxiv_id=f"2607.8200{i}", title=f"Missed {i}", abstract="research"
            )
            session.add(paper)
            await session.flush()
            session.add(DailyFeedEntry(paper_id=paper.id, feed_date=day, primary_category="cs.AI"))
        # 模拟「上次成功同步是 3 天前」——中间那两天漏了
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        state = dict(library.ingest_state or {})
        state["last_run"] = {
            "voyage_id": "x",
            "finished_at": (dt.datetime.now(dt.UTC) - dt.timedelta(days=3)).isoformat(),
        }
        library.ingest_state = state
        await session.commit()

    async with get_sessionmaker()() as session:
        await daily_feed.set_sync_scope(session, "since_last")

    r = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "incremental", "knobs": KNOBS},
        headers=headers,
    )
    eng, _ = _make_engine()
    await eng.run(uuid.UUID(r.json()["id"]))
    obs = (await client.get(f"/api/voyages/{r.json()['id']}", headers=headers)).json()["steps"][0][
        "observation"
    ]
    assert obs["feed_scope"] == "since_last"
    assert obs["feed_since"] == (today - dt.timedelta(days=3)).isoformat()
    assert obs["feed_total"] == 2, "漏掉那两天的论文被补扫回来"


# ---- 相关性不足的论文直接删除，不进回收站 ----


async def _seed_feed(session, project_id, specs):
    """往每日池里放几篇论文（用今天，否则会被 since_last 的窗口筛掉）。返回 id 列表。"""
    import datetime as dt

    from app.models.daily_feed import DailyFeedEntry

    ids = []
    for aid, title, abstract in specs:
        paper = new_paper(
            source="arxiv",
            arxiv_id=aid,
            title=title,
            abstract=abstract,
            year=2026,
            published_at=dt.datetime.now(dt.UTC),
        )
        session.add(paper)
        await session.flush()
        ids.append(paper.id)
        session.add(DailyFeedEntry(paper_id=paper.id, feed_date=_today(), primary_category="cs.AI"))
    await session.commit()
    return ids


async def _run_incremental(client, project_id, headers):
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "incremental", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))
    return (await client.get(f"/api/voyages/{resp.json()['id']}", headers=headers)).json()


async def _bootstrap(client, project_id, headers):
    resp = await client.post(
        f"/api/projects/{project_id}/ingest",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(resp.json()["id"]))


async def test_irrelevant_papers_are_deleted_not_trashed(client, queue_stub, wiki_mocks):
    """相关性不达标的论文直接删成员行，不落回收站；论文本体留在每日池里。

    自动淘汰每天上百篇，堆在回收站里没人会去翻，还会把用户手动删的那几篇淹掉。
    """
    from sqlalchemy import select

    from app.models.library_direction import LibraryPaper
    from app.models.paper import Paper

    project_id, headers = await _setup_project(client)
    await _bootstrap(client, project_id, headers)

    async with get_sessionmaker()() as session:
        kept_id, dropped_id = await _seed_feed(
            session,
            project_id,
            [
                ("2607.95000", "Feed Planning Agent", "research agents and planning"),
                ("2607.95001", "Totally irrelevant paper", "irrelevant to this direction"),
            ],
        )

    detail = await _run_incremental(client, project_id, headers)
    assert detail["status"] == "done", detail

    async with get_sessionmaker()() as session:
        members = {
            m.paper_id: m
            for m in (
                await session.execute(
                    select(LibraryPaper).where(LibraryPaper.paper_id.in_([kept_id, dropped_id]))
                )
            )
            .scalars()
            .all()
        }
        # 达标的留在库里（打分后流水线继续走，状态会推进到 fetched/compiled）
        assert kept_id in members
        assert members[kept_id].status in ("scored", "fetched", "compiled", "included")
        # 关键：不是 excluded，而是**根本没有这条成员行**
        assert dropped_id not in members
        # 论文本体还在——每日池仍然引用着它，别的库也还用得上
        assert await session.get(Paper, dropped_id) is not None


async def test_rejected_papers_are_not_scored_twice(client, queue_stub, wiki_mocks):
    """成员行删了之后仍要挡住重复打分。

    since_last 的扫描窗口和上次同步那天是重叠的，不记一笔的话，今天淘汰的论文
    明天会再花一次 LLM——每库每天几百篇。
    """
    project_id, headers = await _setup_project(client)
    await _bootstrap(client, project_id, headers)

    async with get_sessionmaker()() as session:
        await _seed_feed(
            session,
            project_id,
            [("2607.96000", "Another irrelevant one", "irrelevant to this direction")],
        )

    await _run_incremental(client, project_id, headers)
    after_first = await _relevance_call_count()

    # 同一批池子再同步一次：那篇被淘汰的不该再打一次分
    detail = await _run_incremental(client, project_id, headers)
    assert detail["status"] == "done", detail
    assert await _relevance_call_count() == after_first

    obs = detail["steps"][0]["observation"]
    assert obs["skipped_previously_rejected"] >= 1


# ---- 并发插入冲突（生产死锁：asyncpg DeadlockDetectedError） ----


class _FakeDeadlock(Exception):
    """asyncpg 的死锁异常长这样：DBAPIError.orig 上带 sqlstate=40P01。"""

    sqlstate = "40P01"


def _deadlock_error() -> Exception:
    from sqlalchemy.exc import DBAPIError

    return DBAPIError("INSERT INTO papers ...", {}, _FakeDeadlock("deadlock detected"))


async def test_insert_retry_recovers_from_one_deadlock():
    """第一次死锁、第二次成功 → 返回论文，不上抛。"""

    calls = {"n": 0}
    sentinel = object()

    class _Session:
        async def commit(self):
            return None

        async def rollback(self):
            return None

    async def fake_pool(session, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _deadlock_error()
        return sentinel

    import app.agents.voyage.actions_wiki as mod

    original = mod._pool_paper_or_new
    mod._pool_paper_or_new = fake_pool
    try:
        paper, reason = await mod._insert_pooled_with_retry(_Session(), library_id=None)
    finally:
        mod._pool_paper_or_new = original

    assert paper is sentinel and reason is None
    assert calls["n"] == 2  # 死锁一次 + 重试成功一次


async def test_insert_retry_gives_up_but_does_not_kill_the_step():
    """一直死锁 → 记下原因返回 None，让上层继续处理其余论文，而不是整步失败。"""
    import app.agents.voyage.actions_wiki as mod

    class _Session:
        async def commit(self):
            return None

        async def rollback(self):
            return None

    async def always_deadlock(session, **kwargs):
        raise _deadlock_error()

    original = mod._pool_paper_or_new
    mod._pool_paper_or_new = always_deadlock
    try:
        paper, reason = await mod._insert_pooled_with_retry(_Session(), library_id=None)
    finally:
        mod._pool_paper_or_new = original

    assert paper is None
    assert reason and "40P01" not in reason and "DBAPIError" in reason


async def test_insert_retry_reraises_unrelated_errors():
    """非冲突类错误照抛——别把真 bug 吞成「这一篇跳过」。"""
    import app.agents.voyage.actions_wiki as mod

    class _Session:
        async def commit(self):
            return None

        async def rollback(self):
            return None

    async def boom(session, **kwargs):
        raise ValueError("something else entirely")

    original = mod._pool_paper_or_new
    mod._pool_paper_or_new = boom
    try:
        with pytest.raises(ValueError, match="something else"):
            await mod._insert_pooled_with_retry(_Session(), library_id=None)
    finally:
        mod._pool_paper_or_new = original


# ---- 只处理本次收下的，不捞历史积压 ----


async def test_run_does_not_pick_up_an_older_backlog(client, queue_stub, wiki_mocks):
    """库里早先积压的 scored 论文，不该被这次同步顺手带走。

    以前取全文那步扫的是全库所有 status='scored' 的行，于是「本次通过筛选 1 篇」
    后面跟着「下载 12 个 PDF」，两个数字统计的不是同一批论文。
    """
    import datetime as dt

    from sqlalchemy import select as sa_select

    from app.models.library_direction import LibraryPaper

    project_id, headers = await _setup_project(client)
    await _bootstrap(client, project_id, headers)

    # 造一篇「上周就打过分、一直没取全文」的积压论文
    async with get_sessionmaker()() as session:
        rows = await project_paper_rows(session, project_id=project_id)
        library_id = rows[0][1].library_id
        stale = new_paper(
            source="arxiv", arxiv_id="2607.97000", title="Stale Backlog Paper", year=2026
        )
        session.add(stale)
        await session.flush()
        session.add(
            LibraryPaper(
                library_id=library_id,
                paper_id=stale.id,
                status="scored",
                relevance_score=0.95,  # 分很高，按相关度排序会排在最前
                scored_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=7),
            )
        )
        await session.commit()
        stale_id = stale.id

    async with get_sessionmaker()() as session:
        await _seed_feed(
            session,
            project_id,
            [("2607.97001", "Fresh Feed Paper", "research agents and planning")],
        )

    detail = await _run_incremental(client, project_id, headers)
    assert detail["status"] == "done", detail

    async with get_sessionmaker()() as session:
        stale_row = (
            await session.execute(sa_select(LibraryPaper).where(LibraryPaper.paper_id == stale_id))
        ).scalar_one()
        # 积压那篇原地不动：没被这次运行取全文、也没被编译
        assert stale_row.status == "scored", "历史积压被这次同步顺手带走了"


async def test_digest_only_survives_a_backward_clock_jump(client, queue_stub, wiki_mocks):
    """打分之后墙钟回拨一秒，手动简报仍然选 digest_only，一篇不少。

    本机容器的墙钟每 10 秒跳 ±1 秒（#234 的根因）。此前 create_digest_voyage 的窗口带
    `scored_at <= now` 上界：回拨发生在打分与建简报之间时，now 小于刚打的 scored_at，
    论文掉出窗口——paper_count 少一截，策略翻成 incremental。合法数据里 scored_at
    不会在未来，那个上界什么都不保护。
    """
    import datetime as real_datetime

    from app.services import ingest as ingest_service_mod

    token = await register_and_login(client, email="clock-jump@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    library_id = await _create_standalone_library(client, headers, name="独立库-时钟回拨")

    bootstrap = await client.post(
        f"/api/libraries/{library_id}/ingest/run",
        json={"mode": "bootstrap", "knobs": KNOBS},
        headers=headers,
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(bootstrap.json()["id"]))

    # 模拟回拨：ingest 模块里的"现在"比真实时间早 2 秒——刚打的 scored_at 全在"未来"
    class _RewoundDateTime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return real_datetime.datetime.now(tz) - real_datetime.timedelta(seconds=2)

    original = ingest_service_mod.datetime
    ingest_service_mod.datetime = _RewoundDateTime
    try:
        resp = await client.post(
            f"/api/libraries/{library_id}/digests/generate", headers=headers
        )
    finally:
        ingest_service_mod.datetime = original

    assert resp.status_code == 201, resp.text
    launch = resp.json()
    assert launch["strategy"] == "digest_only", (
        f"回拨两秒就把策略翻成了 {launch['strategy']}——上界又回来了？"
    )
    assert launch["paper_count"] == 2, launch


async def test_next_sync_follows_the_configured_fetch_time(client):
    """「下次自动同步」要跟着每日抓取时刻走。

    库同步是事件驱动的：每日论文抓完、且真有新论文才触发。以前这个时刻写死 03:00 UTC，
    而抓取默认 01:30 UTC——界面报北京 11:00，实际约 09:35 就跑完了。孤立看只是个小数字，
    但它属于「界面说的和实际发生的不是一回事」，用户照着它等，等到的是已经结束的东西。
    """
    import datetime as _dt

    from app.models.library_direction import DirectionLibrary
    from app.services.ingest import next_daily_sync_at

    library = DirectionLibrary(name="x", definition={"statement": "s"})
    library.ingest_state = {"watermark": "2026-08-01T00:00:00+00:00"}

    at = next_daily_sync_at(library, fetch_at=(5, 45))
    assert at is not None and (at.hour, at.minute) == (5, 45)
    # 永远在将来：今天那个时刻已经过了就顺延到明天
    assert at > _dt.datetime.now(_dt.UTC)

    # 没建过库的库没有下次同步（不是给个假时刻）
    library.ingest_state = {}
    assert next_daily_sync_at(library, fetch_at=(5, 45)) is None


async def test_next_sync_reads_the_setting_not_a_hardcoded_hour(client):
    """端到端：改了抓取时刻，库状态里的下次同步跟着变。"""
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary
    from app.models.user import User
    from app.services import daily_feed
    from app.services.ingest import library_ingest_state

    token = await register_and_login(client)
    assert token
    async with get_sessionmaker()() as session:
        await daily_feed.set_sync_time(session, 6, 15)
        user = (await session.execute(select(User))).scalars().first()
        library = DirectionLibrary(name="sync-time-lib", definition={"statement": "s"})
        library.ingest_state = {"watermark": "2026-08-01T00:00:00+00:00"}
        session.add(library)
        await session.commit()
        state = await library_ingest_state(session, library, user=user)

    assert state["next_sync_at"]
    assert state["next_sync_at"][11:16] == "06:15"
