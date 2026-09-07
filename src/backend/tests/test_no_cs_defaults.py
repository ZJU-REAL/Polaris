"""CS 分类学去缺省（#720 审计 A4）+ 裸构造修复的回归。

以前三处缺省把 cs.* 长在代码里：每日订阅回退 cs.AI/cs.CL/cs.CV、wiki 建库
回退 cs.CL/cs.AI/cs.LG、每日列表排序写死 cs.CL/cs.RO。非 CS 用户会被静默灌
计算机论文。现在：没配就是空 + 明确提示，绝不替用户选学科。
"""

import uuid
from types import SimpleNamespace

import pytest

from app.core.db import get_sessionmaker
from app.services import daily_feed
from app.services.literature import reset_clients, set_clients
from tests.conftest import make_project_with_library, register_and_login

pytestmark = pytest.mark.asyncio


class _ExplodingArxiv:
    """没订阅分类时**一次都不该**打数据源；打了就是回退缺省又复活了。"""

    async def fetch_new(self, category: str):
        raise AssertionError(f"unexpected fetch for {category}")


async def test_daily_categories_have_no_code_default(client):
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        # 没配过 = 空，不再回退 cs.AI/cs.CL/cs.CV
        assert await daily_feed.get_categories(session) == []
        # 配了还能清空（空订阅是合法状态，池子停止进新论文）
        await daily_feed.set_categories(session, ["stat.ML"])
        assert await daily_feed.get_categories(session) == ["stat.ML"]
        await daily_feed.set_categories(session, [])
        assert await daily_feed.get_categories(session) == []


async def test_daily_fetch_and_probe_do_nothing_without_subscription(client, monkeypatch):
    await register_and_login(client)
    monkeypatch.setattr(daily_feed, "get_arxiv_client", lambda: _ExplodingArxiv())
    async with get_sessionmaker()() as session:
        categories, by_category, statuses = await daily_feed.fetch_new_by_category(session)
        assert categories == [] and by_category == {} and statuses == {}
        # 探测同理：无可收之物 → 不开同步轮（以前这个分支不可达，语义是「照常探」）
        assert await daily_feed.todays_batch_available(session) == (False, None)


async def test_wiki_bootstrap_refuses_unbounded_search(client, monkeypatch):
    """关键词、分类两头都空：如实报警并跳过检索，而不是回退 cs.* 或全网抓取。"""
    from app.agents.voyage import actions_wiki
    from app.agents.voyage.actions import ActionContext
    from app.core.llm.router import LLMRouter
    from app.models.voyage import VoyageRun

    token = await register_and_login(client, email="no-default-cats@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="no-cats", definition={"statement": "keyword-free library"}
    )
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="wiki_bootstrap",
            status="executing",
            project_id=uuid.UUID(project_id),
            library_id=library_id,
            goal="bootstrap without categories or keywords",
            checkpoint={"params": {"mode": "search", "knobs": {"max_papers": 5}}},
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    monkeypatch.setattr(actions_wiki, "get_arxiv_client", lambda: _ExplodingArxiv())
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
    ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint))
    result = await actions_wiki.search_candidates(ctx, {})
    assert result["found"] == 0 and result["inserted"] == 0
    assert result["diagnostic_status"] == "warning"
    assert any("关键词" in message for message in result["diagnostic_messages"])
    # 有明确条件时照常检索（见 test_wiki_keyword_only_search_omits_category_filter）


async def test_wiki_keyword_only_search_omits_category_filter(client, monkeypatch):
    """definition 只有关键词：检索照跑，但不带任何分类过滤（更不带 cs.* 缺省）。"""
    from app.agents.voyage import actions_wiki
    from app.agents.voyage.actions import ActionContext
    from app.core.llm.router import LLMRouter
    from app.models.voyage import VoyageRun

    class _RecordingArxiv:
        page_size = 100

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def search_page(self, **kwargs):
            self.calls.append(dict(kwargs))
            return []

    token = await register_and_login(client, email="kw-only-cats@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client,
        headers,
        name="kw-only",
        definition={"statement": "keyword only", "keywords": {"include": ["spintronics"]}},
    )
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="wiki_bootstrap",
            status="executing",
            project_id=uuid.UUID(project_id),
            library_id=library_id,
            goal="keyword-only bootstrap",
            checkpoint={"params": {"mode": "search", "knobs": {"max_papers": 5}}},
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    arxiv = _RecordingArxiv()
    monkeypatch.setattr(actions_wiki, "get_arxiv_client", lambda: arxiv)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
    ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint))
    result = await actions_wiki.search_candidates(ctx, {})
    assert result["found"] == 0
    assert len(arxiv.calls) == 1
    assert arxiv.calls[0]["categories"] == []
    assert arxiv.calls[0]["keywords"] == ["spintronics"]


async def test_daily_list_ranks_by_subscription_order(client, monkeypatch):
    """列表分类优先级 = 订阅顺序（以前写死 cs.CL 最前 / cs.RO 最后）。"""
    from tests.test_daily_feed import _rss_entry, _StubArxiv

    token = await register_and_login(client, email="rank-order@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {
                "q-bio.NC": [_rss_entry("2608.31001", "Neuro Paper")],
                "stat.ML": [_rss_entry("2608.31002", "Stats Paper")],
            }
        ),
    )
    async with get_sessionmaker()() as session:
        # 非 CS 订阅照样有先后：排订阅列表前面的分类先出现
        await daily_feed.set_categories(session, ["stat.ML", "q-bio.NC"])
        _, by_category, _ = await daily_feed.fetch_new_by_category(session)
        await daily_feed.upsert_entries(session, by_category=by_category)

    resp = await client.get("/api/daily/papers", params={"sort": "date"}, headers=headers)
    titles = [item["title"] for item in resp.json()["items"]]
    assert titles.index("Stats Paper") < titles.index("Neuro Paper"), titles


async def test_proposal_external_search_uses_shared_singletons():
    """裸构造修复：外部检索走 set_clients 可注入的模块级单例，而非私建客户端。"""
    from app.agents.voyage.actions_proposal import _external_search

    class _S2:
        async def search_papers(self, query, *, limit):
            return [{"title": f"S2 hit for {query}", "year": 2026, "url": "https://s2"}]

    class _OpenAlex:
        async def search_works(self, query, *, limit):  # pragma: no cover - S2 命中则不落到这
            raise AssertionError("openalex fallback should not run")

    set_clients(s2=_S2(), openalex=_OpenAlex())  # type: ignore[arg-type]
    try:
        ctx = SimpleNamespace(bus=None, run=None)
        results, ok = await _external_search(ctx, ["quantum sensing"], limit=3)
    finally:
        reset_clients()
    assert ok and [r["title"] for r in results] == ["S2 hit for quantum sensing"]


async def test_writing_related_candidates_use_shared_singleton():
    from app.agents.voyage.actions_writing import _s2_candidates

    class _S2:
        async def search_papers(self, query, *, limit):
            return [{"title": "Related", "year": 2025, "authors": [{"name": "A"}]}]

        async def aclose(self):  # pragma: no cover — 单例绝不能被这条链路关掉
            raise AssertionError("shared client must not be closed")

    set_clients(s2=_S2())  # type: ignore[arg-type]
    try:
        candidates = await _s2_candidates("some manuscript title")
    finally:
        reset_clients()
    assert candidates and candidates[0]["title"] == "Related"
