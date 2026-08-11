"""Regressions for shared arXiv query cooldown and resumable paging."""

import uuid

import fakeredis.aioredis
import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.agents.voyage import actions_wiki
from app.agents.voyage.actions import ActionContext
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.library_direction import LibraryPaper
from app.models.voyage import VoyageRun
from app.services.literature.arxiv import ArxivClient, ArxivRateLimitedError
from tests.conftest import make_project_with_library, register_and_login


@respx.mock
async def test_arxiv_429_sets_global_query_cooldown():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    client = ArxivClient(redis=redis, min_interval=0, max_retries=1, backoff_base=0)
    route = respx.get("https://export.arxiv.org/api/query").mock(return_value=httpx.Response(429))
    with pytest.raises(ArxivRateLimitedError) as first:
        await client.search_page(keywords=["agents"], limit=1)
    assert first.value.retry_after == 600
    with pytest.raises(ArxivRateLimitedError) as second:
        await client.search_page(keywords=["agents"], limit=1)
    assert second.value.retry_after and second.value.retry_after > 0
    assert route.call_count == 1
    await redis.aclose()


async def test_arxiv_search_resumes_after_the_last_committed_page(client, monkeypatch):
    """A failed second page resumes at its saved offset without replaying page one."""

    class InterruptingArxiv:
        # 和真客户端一样对外报页大小。调用方按它要页并据此判末页，替身少了这个属性，
        # 测试就测不到真实的分页终止路径。
        page_size = 100

        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.interrupted = False
            self.entries = [
                {
                    "arxiv_id": f"2608.{index:05d}",
                    "title": f"Resumable arXiv result {index}",
                    "abstract": f"A deterministic resumable-search paper {index}.",
                    "authors": [f"Author {index}"],
                    "year": 2026,
                    "primary_category": "cs.AI",
                    "url": f"https://arxiv.org/abs/2608.{index:05d}",
                    "published": "2026-08-01T00:00:00+00:00",
                }
                for index in range(101)
            ]

        async def search_page(self, **kwargs):
            self.calls.append(dict(kwargs))
            start = kwargs["start"]
            if start == 100 and not self.interrupted:
                self.interrupted = True
                raise ArxivRateLimitedError("simulated second-page limit", retry_after=600)
            return self.entries[start : start + kwargs["limit"]]

    token = await register_and_login(client, email="arxiv-resume@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client,
        headers,
        name="arxiv-resume",
        definition={
            "statement": "Resumable literature search",
            "keywords": {"arxiv_categories": ["cs.AI"], "include": ["agents"]},
        },
    )
    checkpoint = {
        "params": {
            "mode": "search",
            "knobs": {"max_papers": 101, "months_back": 6},
        }
    }
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="wiki_bootstrap",
            status="executing",
            project_id=uuid.UUID(project_id),
            library_id=library_id,
            goal="Resume a paginated arXiv search",
            checkpoint=dict(checkpoint),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    arxiv = InterruptingArxiv()
    monkeypatch.setattr(actions_wiki, "get_arxiv_client", lambda: arxiv)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
    first_ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint))
    with pytest.raises(ArxivRateLimitedError):
        await actions_wiki.search_candidates(first_ctx, {})

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        saved = run.checkpoint["arxiv_search_resume"]
        inserted = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(LibraryPaper)
                    .where(LibraryPaper.library_id == library_id)
                )
            ).scalar_one()
        )
    assert saved["next_start"] == saved["fetched"] == saved["inserted"] == inserted == 100
    assert saved["done"] is False

    resume_ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint))
    result = await actions_wiki.search_candidates(resume_ctx, {})

    assert [call["start"] for call in arxiv.calls] == [0, 100, 100]
    assert arxiv.calls[0]["since"] == arxiv.calls[2]["since"]
    assert arxiv.calls[0]["until"] == arxiv.calls[2]["until"]
    assert result["found"] == result["inserted"] == 101
    # 清单跨续跑累计：续跑那一轮内存里只有最后一页的 1 篇，但 observation 报的是
    # 整次搜索的 101 篇。两者对不上时，看的人无从判断是不是真丢了 100 篇。
    assert result["new_papers"], "续跑后清单不能是空的"
    assert result["new_papers"][0]["title"] == "Resumable arXiv result 0", (
        "清单该从第一页开始，而不是只剩续跑那一页"
    )
    assert len({item["id"] for item in result["new_papers"]}) == len(result["new_papers"])

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        completed = run.checkpoint["arxiv_search_resume"]
        inserted = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(LibraryPaper)
                    .where(LibraryPaper.library_id == library_id)
                )
            ).scalar_one()
        )
    assert completed["next_start"] == completed["fetched"] == inserted == 101
    assert completed["done"] is True


@respx.mock
async def test_arxiv_403_does_not_freeze_everyone_elses_search():
    """403 不触发平台级冷却。

    冷却是**单键、全平台**的：一个人的建库搜索踩到它，所有人的检索都停。所以触发
    条件必须是对方明确说「你太快了」（429）。arXiv 的 403 更常见的原因是 UA/来源
    被拦——那种情况等十分钟不会变好，却把整个实验室连坐了。
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    client = ArxivClient(redis=redis, min_interval=0, max_retries=1, backoff_base=0)
    route = respx.get("https://export.arxiv.org/api/query").mock(return_value=httpx.Response(403))

    with pytest.raises(ArxivRateLimitedError) as first:
        await client.search_page(keywords=["agents"], limit=1)
    assert first.value.retry_after is None, "403 不该开冷却"

    # 下一次调用仍然真的打出去（没有被冷却挡在门外）
    with pytest.raises(ArxivRateLimitedError):
        await client.search_page(keywords=["agents"], limit=1)
    assert route.call_count == 2
    await redis.aclose()


@respx.mock
async def test_cooldown_honours_a_short_retry_after():
    """服务器说等 5 秒就等 5 秒，不要一律按 10 分钟顶格。

    这个冷却冻的是所有人的检索，分寸该由对方定；顶格只是在没给建议时的兜底。
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    client = ArxivClient(redis=redis, min_interval=0, max_retries=1, backoff_base=0)
    respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "5"})
    )
    with pytest.raises(ArxivRateLimitedError) as caught:
        await client.search_page(keywords=["agents"], limit=1)
    assert caught.value.retry_after == 5, "Retry-After 被无视了"
    await redis.aclose()
