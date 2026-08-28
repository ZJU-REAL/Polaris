"""Issue #473: execute source adapters and persist ranked candidates."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

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


async def _create_run(client, *, source_config, requested_count=2, candidate_budget=5):
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
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"]), headers, response.json()["library_id"]


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
        assert run.progress == {
            "phase": "completed",
            "source": "core",
            "fetched": 4,
            "accepted": 2,
            "requested_count": 2,
            "candidate_budget": 5,
            "returned_count": 2,
        }
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
            "core": "partial",
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
    assert [request.limit for request in semantic.requests] == [5]


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
