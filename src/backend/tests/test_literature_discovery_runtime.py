"""Issue #473: execute source adapters and persist ranked candidates."""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models.literature_discovery import (
    LiteratureSearchHit,
    LiteratureSourceAttempt,
)
from app.models.paper import Paper
from app.schemas.literature_discovery import (
    LiteratureCandidate,
    SourceSearchPage,
    SourceSearchRequest,
)
from app.services.literature import oa_cache
from app.services.literature import runtime as runtime_service
from app.services.literature.multi_source import (
    MultiSourceClient,
    ProviderRequestError,
    _pubmed_abstracts,
)
from app.services.literature.openalex import _simplify
from app.services.literature.runtime import (
    AdapterRegistry,
    MultiSourceAdapter,
    OpenAlexAdapter,
    SemanticScholarAdapter,
    _candidate_from_openalex,
    run_discovery,
)
from tests.conftest import register_and_login


class FakeAdapter:
    def __init__(
        self,
        name: str,
        items: list[LiteratureCandidate],
        *,
        error: Exception | None = None,
    ):
        self.name = name
        self.items = items
        self.error = error
        self.requests = []

    async def search(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return SourceSearchPage(source=self.name, items=self.items, fetched_count=len(self.items))


def _candidate(source: str, title: str, *, doi: str | None = None, year: int = 2024):
    return LiteratureCandidate(
        source=source,
        title=title,
        abstract=f"Abstract for {title}",
        authors=[{"name": "A. Author"}],
        year=year,
        venue="Journal of Tests",
        doi=doi,
        url=f"https://example.test/{source}",
    )


async def _create_run(
    client, *, source_config, requested_count=2, candidate_budget=5, query_plan=None
):
    token = await register_and_login(client, email=f"runtime-{uuid.uuid4().hex}@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    library = await client.post(
        "/api/libraries",
        json={"name": "Runtime library", "statement": "Runtime test"},
        headers=headers,
    )
    assert library.status_code == 201, library.text
    response = await client.post(
        f"/api/libraries/{library.json()['id']}/literature/runs",
        json={
            "requested_count": requested_count,
            "candidate_budget": candidate_budget,
            "start_year": 2016,
            "end_year": 2025,
            "topic": "structural impact response",
            "source_config": source_config,
            "query_plan": query_plan,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"]), headers, response.json()["library_id"]


class RetrievalRouter:
    async def complete(self, stage, messages, **kwargs):
        del messages, kwargs
        assert stage == "extract"
        return SimpleNamespace(
            content=json.dumps(
                {
                    "queries": [
                        {
                            "seed_id": "core",
                            "purpose": "core",
                            "query": '"structural impact" AND response',
                        },
                        {
                            "seed_id": "coverage",
                            "purpose": "coverage",
                            "query": '"damage mechanics" OR failure',
                        },
                    ]
                }
            ),
            model="query-model",
        )

    async def rerank(self, query, documents, **kwargs):
        del query, documents, kwargs
        raise NotImplementedError


class InvalidQueryRouter:
    async def complete(self, stage, messages, **kwargs):
        del stage, messages, kwargs
        return SimpleNamespace(content="not-json", model="query-model")

    async def rerank(self, query, documents, **kwargs):
        del query, documents, kwargs
        raise AssertionError("reranking must not run without a valid query")


@pytest.mark.asyncio
async def test_runtime_executes_multiple_queries_with_aggregate_budget_and_year_filter(client):
    run_id, _, _ = await _create_run(
        client,
        source_config={"sources": ["openalex"]},
        requested_count=3,
        candidate_budget=6,
        query_plan={
            "queries": [
                {"id": "core", "purpose": "core", "query": "structural impact"},
                {"id": "coverage", "purpose": "coverage", "query": "damage mechanics"},
            ]
        },
    )
    adapter = FakeAdapter(
        "openalex",
        [
            _candidate("openalex", "Out of range", year=2015),
            _candidate("openalex", "In range", year=2020),
        ],
    )

    async with get_sessionmaker()() as session:
        run = await run_discovery(
            session,
            run_id,
            registry=AdapterRegistry((adapter,)),
            llm_router=RetrievalRouter(),
            now=datetime(2026, 8, 26, tzinfo=UTC),
        )
        hits = list(
            (
                await session.execute(
                    select(LiteratureSearchHit).where(LiteratureSearchHit.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )

    assert len(adapter.requests) == 2
    assert sum(request.limit for request in adapter.requests) == 6
    assert all(request.start_year == 2016 for request in adapter.requests)
    assert all(request.end_year == 2025 for request in adapter.requests)
    assert {hit.title for hit in hits} == {"In range"}
    assert len(hits[0].metadata_snapshot["retrieval_hits"]) == 2
    assert run.query_plan["ranking"]["mode"] == "deterministic_fallback"


@pytest.mark.asyncio
async def test_runtime_fails_instead_of_running_a_generic_query_when_generation_is_invalid(client):
    run_id, _, _ = await _create_run(
        client,
        source_config={"sources": ["openalex"], "keywords": ["结构响应"]},
        requested_count=3,
        candidate_budget=6,
    )
    adapter = FakeAdapter("openalex", [_candidate("openalex", "Must not be fetched")])

    async with get_sessionmaker()() as session:
        queued = await session.get(runtime_service.LiteratureSearchRun, run_id)
        queued.topic = "基于视觉模型的结构冲击损伤识别"
        await session.commit()
        run = await run_discovery(
            session,
            run_id,
            registry=AdapterRegistry((adapter,)),
            llm_router=InvalidQueryRouter(),
        )

    assert run.status == "failed"
    assert run.error_summary == "QUERY_GENERATION_FAILED"
    assert run.progress["error_code"] == "QUERY_GENERATION_FAILED"
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_runtime_is_idempotent_after_completion(client):
    run_id, _, _ = await _create_run(
        client,
        source_config={"sources": ["openalex"]},
        requested_count=1,
        candidate_budget=1,
    )
    adapter = FakeAdapter("openalex", [_candidate("openalex", "Stable result")])
    async with get_sessionmaker()() as session:
        first = await run_discovery(
            session,
            run_id,
            registry=AdapterRegistry((adapter,)),
            llm_router=RetrievalRouter(),
        )
        second = await run_discovery(
            session,
            run_id,
            registry=AdapterRegistry((adapter,)),
            llm_router=RetrievalRouter(),
        )
        hit_count = await session.scalar(
            select(func.count())
            .select_from(LiteratureSearchHit)
            .where(LiteratureSearchHit.run_id == run_id)
        )

    assert first.id == second.id
    assert second.status == "completed"
    assert hit_count == 1
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_runtime_does_not_execute_a_run_already_claimed_by_another_worker(client):
    run_id, _, _ = await _create_run(
        client,
        source_config={"sources": ["openalex"]},
        requested_count=1,
        candidate_budget=1,
    )
    adapter = FakeAdapter("openalex", [_candidate("openalex", "Must not be fetched")])
    async with get_sessionmaker()() as session:
        claimed = await session.get(runtime_service.LiteratureSearchRun, run_id)
        assert claimed is not None
        claimed.status = "running"
        await session.commit()
        run = await run_discovery(
            session,
            run_id,
            registry=AdapterRegistry((adapter,)),
            llm_router=RetrievalRouter(),
        )

    assert run.status == "running"
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_runtime_attempts_oa_resolution_for_doi_only_candidates(client, monkeypatch):
    run_id, _, _ = await _create_run(
        client,
        source_config={"sources": ["openalex"]},
        requested_count=1,
        candidate_budget=1,
    )
    adapter = FakeAdapter(
        "openalex", [_candidate("openalex", "DOI resolver candidate", doi="10.1000/oa")]
    )
    observed: list[str | None] = []

    async def fake_cache_hit_pdf(session, hit):
        del session
        observed.append(hit.doi)
        return None

    monkeypatch.setattr(oa_cache, "cache_hit_pdf", fake_cache_hit_pdf)
    async with get_sessionmaker()() as session:
        await run_discovery(
            session,
            run_id,
            registry=AdapterRegistry((adapter,)),
            llm_router=RetrievalRouter(),
        )

    assert observed == ["10.1000/oa"]


@pytest.mark.asyncio
async def test_runtime_persists_versioned_metric_snapshot_and_uses_impact(client, monkeypatch):
    run_id, _, _ = await _create_run(
        client,
        source_config={"sources": ["openalex"]},
        requested_count=1,
        candidate_budget=1,
    )
    candidate = _candidate("openalex", "Metric candidate")
    candidate.venue = "Journal of Tests"
    candidate.citation_count = 0

    class MetricService:
        async def enrich_candidates(self, session, candidates):
            del session
            enriched = [dict(item) for item in candidates]
            enriched[0]["impact_score"] = 1.0
            enriched[0]["venue_metric_snapshot"] = {
                "version": "venue-metrics-v1",
                "identity": "name:journaloftests",
                "metrics": {"impact_factor": 10.0},
            }
            return enriched

    async def no_oa_cache(*_args, **_kwargs):
        return None

    monkeypatch.setattr(oa_cache, "cache_hit_pdf", no_oa_cache)
    async with get_sessionmaker()() as session:
        run = await run_discovery(
            session,
            run_id,
            registry=AdapterRegistry((FakeAdapter("openalex", [candidate]),)),
            llm_router=RetrievalRouter(),
            venue_metric_service=MetricService(),
        )
        hit = await session.scalar(
            select(LiteratureSearchHit).where(LiteratureSearchHit.run_id == run.id)
        )

    assert hit is not None
    assert hit.venue_metric_snapshot["version"] == "venue-metrics-v1"
    assert hit.scores["impact"] == 1.0


@pytest.mark.asyncio
async def test_runtime_persists_progress_as_each_provider_query_completes(client):
    run_id, _, _ = await _create_run(
        client,
        source_config={"sources": ["openalex", "semantic"]},
        requested_count=1,
        candidate_budget=2,
    )
    release_slow = asyncio.Event()
    fast_finished = asyncio.Event()

    class FastAdapter(FakeAdapter):
        async def search(self, request):
            result = await super().search(request)
            fast_finished.set()
            return result

    class SlowAdapter(FakeAdapter):
        async def search(self, request):
            await release_slow.wait()
            return await super().search(request)

    registry = AdapterRegistry(
        (
            FastAdapter("openalex", [_candidate("openalex", "Fast result")]),
            SlowAdapter("semantic", [_candidate("semantic", "Slow result")]),
        )
    )
    async with get_sessionmaker()() as worker_session:
        execution = asyncio.create_task(
            run_discovery(
                worker_session,
                run_id,
                registry=registry,
                llm_router=RetrievalRouter(),
            )
        )
        await asyncio.wait_for(fast_finished.wait(), timeout=2)
        observed_completed = 0
        for _ in range(20):
            async with get_sessionmaker()() as observer_session:
                observed = await observer_session.get(runtime_service.LiteratureSearchRun, run_id)
                observed_completed = int((observed.progress or {}).get("query_completed") or 0)
            if observed_completed:
                break
            await asyncio.sleep(0.05)
        assert observed_completed >= 1
        release_slow.set()
        await asyncio.wait_for(execution, timeout=3)


@pytest.mark.asyncio
async def test_runtime_deduplicates_isolates_failures_and_persists_progress(client):
    run_id, _, _ = await _create_run(
        client,
        source_config={"sources": ["openalex", "semantic", "arxiv", "crossref", "core"]},
        requested_count=2,
        candidate_budget=5,
    )
    duplicate_doi = "10.1234/shared"
    openalex = FakeAdapter(
        "openalex",
        [
            _candidate("openalex", "Shared impact study", doi=duplicate_doi),
            _candidate("openalex", "Unique study", year=2023),
        ],
    )
    semantic = FakeAdapter(
        "semantic",
        [_candidate("semantic", "Shared impact study", doi=duplicate_doi, year=2022)],
    )
    arxiv = FakeAdapter("arxiv", [_candidate("arxiv", "Exploratory arXiv study", year=2025)])
    core = FakeAdapter("core", [], error=RuntimeError("core unavailable"))

    async with get_sessionmaker()() as session:
        run = await run_discovery(
            session,
            run_id,
            registry=AdapterRegistry((openalex, semantic, arxiv, core)),
            now=datetime(2026, 8, 26, tzinfo=UTC),
        )
        assert run.status == "partial"
        assert run.progress["phase"] == "completed"
        assert run.progress["source"] == "core"
        assert run.progress["fetched"] == 4
        assert run.progress["accepted"] == 2
        assert run.progress["requested_count"] == 2
        assert run.progress["candidate_budget"] == 5
        assert run.progress["returned_count"] == 2
        assert run.progress["deduplicated"] == 3
        assert run.progress["pending_rerank"] == 0
        assert run.progress["ranked_count"] == 3
        assert run.progress["start_year"] == 2016
        assert run.progress["end_year"] == 2025
        attempts = list(
            (
                await session.execute(
                    select(LiteratureSourceAttempt).where(LiteratureSourceAttempt.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        assert {item.source: item.status for item in attempts} == {
            "openalex": "completed",
            "semantic": "completed",
            "arxiv": "completed",
            "crossref": "skipped",
            "core": "failed",
        }
        hits = list(
            (
                await session.execute(
                    select(LiteratureSearchHit).where(LiteratureSearchHit.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(hits) == 2
        shared = next(hit for hit in hits if hit.doi == duplicate_doi)
        assert shared.metadata_snapshot["sources"] == ["openalex", "semantic"]
        assert await session.scalar(select(func.count()).select_from(Paper)) == 0

    assert [request.start_year for request in openalex.requests] == [2016]
    assert [request.end_year for request in arxiv.requests] == [2025]
    assert [request.limit for request in semantic.requests] == [1]


@pytest.mark.asyncio
async def test_runtime_marks_missing_sources_as_failed(client):
    run_id, _, _ = await _create_run(client, source_config={"sources": []})
    async with get_sessionmaker()() as session:
        run = await run_discovery(session, run_id, registry=AdapterRegistry())
        assert run.status == "failed"
        assert run.error_summary == "NO_SOURCES_CONFIGURED"
        assert run.progress["requested_count"] == run.requested_count
        assert run.progress["candidate_budget"] == run.candidate_budget
        assert run.progress["returned_count"] == 0


@pytest.mark.asyncio
async def test_start_endpoint_enqueues_without_overwriting_requested_count(client, queue_stub):
    run_id, headers, library_id = await _create_run(
        client,
        source_config={"sources": ["openalex"]},
        requested_count=7,
        candidate_budget=80,
    )
    response = await client.post(
        f"/api/libraries/{library_id}/literature/runs/{run_id}/start",
        headers=headers,
    )
    assert response.status_code == 202, response.text
    assert response.json()["requested_count"] == 7
    assert queue_stub.jobs == [("run_literature_discovery", (str(run_id),), {})]


@pytest.mark.asyncio
async def test_provider_adapters_forward_year_window_and_restore_openalex_abstract():
    class Client:
        def __init__(self):
            self.calls = []

        async def search_works(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return []

        async def search_papers(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return []

    request = type(
        "Request",
        (),
        {"query": "impact response", "limit": 12, "start_year": 2016, "end_year": 2020},
    )()
    openalex_client = Client()
    semantic_client = Client()
    await OpenAlexAdapter(openalex_client).search(request)
    await SemanticScholarAdapter(semantic_client).search(request)
    assert openalex_client.calls[0][1] == {
        "limit": 12,
        "start_year": 2016,
        "end_year": 2020,
    }
    assert semantic_client.calls[0][1] == {
        "limit": 12,
        "start_year": 2016,
        "end_year": 2020,
    }

    candidate = _candidate_from_openalex(
        _simplify(
            {
                "title": "Indexed abstract",
                "abstract_inverted_index": {"impact": [1], "Dynamic": [0], "response": [2]},
            }
        )
    )
    assert candidate.abstract == "Dynamic impact response"


@pytest.mark.asyncio
async def test_extended_sources_share_candidate_contract_and_keep_unpaywall_as_resolver():
    class Client:
        async def search_source(self, source, request):
            assert source == "crossref"
            assert request.start_year == 2016
            return [
                {
                    "title": "Crossref result",
                    "abstract": "An abstract",
                    "authors": [{"name": "Author"}],
                    "year": 2020,
                    "venue": "Journal",
                    "doi": "10.1000/example",
                    "metadata": {"source_id": "cr-1"},
                }
            ]

    request = type(
        "Request",
        (),
        {"query": "topic", "limit": 10, "start_year": 2016, "end_year": 2025},
    )()
    page = await MultiSourceAdapter("crossref", Client()).search(request)
    assert page.fetched_count == 1
    assert page.items[0].source == "crossref"
    assert page.items[0].doi == "10.1000/example"
    assert page.items[0].metadata == {"source_id": "cr-1"}
    assert await MultiSourceClient(client=Client()).search_source("unpaywall", request) == []


@pytest.mark.asyncio
async def test_runtime_persists_provider_error_instead_of_reporting_zero_hit_success(client):
    run_id, _, _ = await _create_run(
        client,
        source_config={"sources": ["pubmed"]},
        requested_count=5,
        candidate_budget=10,
    )

    class BrokenAdapter:
        name = "pubmed"

        async def search(self, request):
            raise ProviderRequestError(
                "pubmed", "HTTP_503", "PubMed temporarily unavailable", retryable=True
            )

    async with get_sessionmaker()() as session:
        run = await run_discovery(session, run_id, registry=AdapterRegistry((BrokenAdapter(),)))
        attempt = await session.scalar(
            select(LiteratureSourceAttempt).where(LiteratureSourceAttempt.run_id == run_id)
        )

    assert run.status == "failed"
    assert run.progress["returned_count"] == 0
    assert attempt.status == "failed"
    assert attempt.error_code == "HTTP_503"
    assert attempt.retryable is True
    assert "HTTP_503" in (run.error_summary or "")


@pytest.mark.asyncio
async def test_multi_source_retry_error_does_not_expose_query_credentials(monkeypatch):
    secret = "SECRET_SENTINEL"
    settings = get_settings()
    monkeypatch.setattr(settings, "literature_source_retries", 0, raising=False)

    class BrokenClient:
        async def request(self, method, url, **kwargs):
            del method, url, kwargs
            request = httpx.Request(
                "GET", f"https://eutils.ncbi.nlm.nih.gov/esearch?api_key={secret}"
            )
            return httpx.Response(503, request=request)

    provider = MultiSourceClient(client=BrokenClient())
    with pytest.raises(ProviderRequestError) as caught:
        await provider._request_json(
            "pubmed",
            "GET",
            "https://eutils.ncbi.nlm.nih.gov/esearch",
            params={"api_key": secret},
        )

    assert caught.value.code == "HTTP_503"
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_runtime_builds_default_registry_from_decrypted_admin_settings(client, monkeypatch):
    run_id, _, _ = await _create_run(
        client,
        source_config={"sources": ["pubmed"]},
        requested_count=1,
        candidate_budget=3,
    )
    adapter = FakeAdapter("pubmed", [_candidate("pubmed", "Configured provider")])
    runtime_settings = {
        "sources": ["pubmed"],
        "provider_keys": {"pubmed": ["decrypted-key"]},
    }
    observed = {}

    async def fake_runtime_settings(session):
        return runtime_settings

    async def fake_registry(settings):
        observed.update(settings)
        return AdapterRegistry((adapter,))

    monkeypatch.setattr(
        runtime_service.literature_settings, "get_runtime_settings", fake_runtime_settings
    )
    monkeypatch.setattr(runtime_service, "build_adapter_registry", fake_registry)

    async with get_sessionmaker()() as session:
        run = await run_discovery(session, run_id)

    assert run.status == "completed"
    assert observed["provider_keys"] == {"pubmed": ["decrypted-key"]}
    assert adapter.requests[0].limit == 3


def test_multi_source_key_pool_rotates_and_pubmed_xml_keeps_full_abstract():
    client = MultiSourceClient(
        client=object(), provider_keys={"provider-under-test": ["key-a", "key-b"]}
    )
    assert {
        client._key("provider-under-test"),
        client._key("provider-under-test"),
    } == {"key-a", "key-b"}

    abstracts = _pubmed_abstracts(
        """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID>
        <Article><Abstract><AbstractText Label="BACKGROUND">First sentence.</AbstractText>
        <AbstractText>Second sentence.</AbstractText></Abstract></Article>
        </MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    )
    assert abstracts == {"123": "BACKGROUND: First sentence.\nSecond sentence."}


def test_explicit_empty_key_pool_does_not_fall_back_to_environment_key():
    client = MultiSourceClient(client=object(), provider_keys={"core": []})

    assert client._key("core", "environment-key") == ""
    assert client._key("unconfigured", "environment-key") == "environment-key"


@pytest.mark.asyncio
async def test_pubmed_adapter_fetches_abstract_and_forwards_years_and_admin_key():
    class Response:
        status_code = 200

        def __init__(self, *, payload=None, text=""):
            self._payload = payload
            self.text = text

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        def __init__(self):
            self.calls = []

        async def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if "esearch" in url:
                return Response(payload={"esearchresult": {"idlist": ["123"]}})
            if "esummary" in url:
                return Response(
                    payload={
                        "result": {
                            "123": {
                                "title": "PubMed full abstract",
                                "pubdate": "2020",
                                "authors": [{"name": "A. Author"}],
                                "articleids": [{"idtype": "doi", "value": "10.1/pubmed"}],
                            }
                        }
                    }
                )
            return Response(
                text=(
                    "<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID>"
                    "<Article><Abstract><AbstractText>Full indexed abstract.</AbstractText>"
                    "</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
                )
            )

    http_client = Client()
    client = MultiSourceClient(client=http_client, provider_keys={"pubmed": ["admin-pubmed-key"]})
    rows = await client.search_source(
        "pubmed",
        SourceSearchRequest(query="impact response", start_year=2016, end_year=2025, limit=5),
    )

    assert rows[0]["abstract"] == "Full indexed abstract."
    search_params = http_client.calls[0][2]["params"]
    assert search_params["term"] == "(impact response) AND (2016:2025[pdat])"
    assert search_params["api_key"] == "admin-pubmed-key"
    assert all(call[2]["params"]["api_key"] == "admin-pubmed-key" for call in http_client.calls)


async def test_provider_keys_never_reach_the_run_snapshot(client):
    """请求里带 provider_keys 也不能落进 run 快照。

    凭据只应由 worker 从管理端设置里解密取用。快照是会被读取、导出、进日志的，
    一旦混进明文 key，泄漏点就从"一处解密"扩散成"任何读 run 的地方"，而且没有任何
    报错提示——所以这条不变量必须有测试钉着，不能只靠建 run 时那一行 pop。
    """
    import uuid as _uuid

    from app.core.db import get_sessionmaker
    from app.models.literature_discovery import LiteratureSearchRun

    token = await register_and_login(client, email="provider-keys@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    library = await client.post(
        "/api/libraries", json={"name": "L", "statement": "s"}, headers=headers
    )
    library_id = library.json()["id"]

    created = await client.post(
        f"/api/libraries/{library_id}/literature/runs",
        json={
            "topic": "t",
            "source_config": {
                "sources": ["openalex"],
                "provider_keys": {"openalex": ["sk-SECRET-LEAK"]},
            },
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text

    async with get_sessionmaker()() as session:
        run = await session.get(LiteratureSearchRun, _uuid.UUID(created.json()["id"]))
        persisted = repr(run.source_config) + repr(run.query_plan) + repr(run.progress)
    assert "SECRET-LEAK" not in persisted, persisted
    assert "provider_keys" not in (run.source_config or {})


def test_disabled_credential_pool_does_not_fall_back_to_env():
    """管理员把某个源的凭据池清空 = 停用，不能再回落到环境变量里的 key。

    只判断池子空不空的话，「配了但清空」和「压根没配」就没区别：停用等于无效，
    请求照样用环境凭据发出去，而且从外面完全看不出来——账单和配额会先于任何
    报错告诉你这件事。
    """
    from app.services.literature.runtime import _credential_pool

    # 没配过这个源 → 允许回落到环境凭据
    assert _credential_pool({}, "openalex", "env-key") == ["env-key"]
    assert _credential_pool({"provider_keys": {}}, "openalex", "env-key") == ["env-key"]

    # 配了但清空 = 显式停用 → 不回落
    assert _credential_pool({"provider_keys": {"openalex": []}}, "openalex", "env-key") == []
    assert _credential_pool({"provider_keys": {"openalex": ["  "]}}, "openalex", "env-key") == []

    # 配了且非空 → 用配置的
    assert _credential_pool(
        {"provider_keys": {"openalex": ["a", "b"]}}, "openalex", "env-key"
    ) == ["a", "b"]
