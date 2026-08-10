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
