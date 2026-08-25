"""Provider contracts, Crossref, and canonical paper identity imports."""

import uuid

import fakeredis.aioredis
import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper import Paper, PaperIdentifier
from app.services import paper_import
from app.services.literature.contracts import Identifier, ImportInput, SearchQuery
from app.services.literature.crossref import CrossrefClient
from app.services.literature.orchestration import (
    load_provider_resume,
    search_query_signature,
    store_provider_resume,
)
from app.services.literature.providers import (
    CrossrefProvider,
    OpenAlexProvider,
    SemanticScholarProvider,
)
from app.services.paper_identity import normalize_identifier


def test_identifier_normalization_and_legacy_import_input():
    assert normalize_identifier("DOI", "https://doi.org/10.1000/ABC") == Identifier(
        "doi", "10.1000/abc"
    )
    assert normalize_identifier("arxiv", "https://arxiv.org/abs/2406.00001v2") == Identifier(
        "arxiv", "2406.00001"
    )
    assert normalize_identifier("pmcid", "1234") == Identifier("pmcid", "PMC1234")
    assert normalize_identifier("corpus_id", "13756489") == Identifier(
        "s2_corpus", "13756489"
    )
    assert paper_import.legacy_import_input(doi="10.1/X") == ImportInput("doi", "10.1/X")
    assert paper_import.legacy_import_input(corpus_id="13756489") == ImportInput(
        "corpus_id", "13756489"
    )
    with pytest.raises(paper_import.ParseFailedError):
        paper_import.legacy_import_input(arxiv_id="2401.1", doi="10.1/x")


def test_provider_checkpoint_migrates_without_dropping_legacy_shape():
    legacy = {"query_signature": "old", "next_start": 25, "done": False}
    checkpoint = {"arxiv_search_resume": legacy}
    assert load_provider_resume(
        checkpoint, provider_id="arxiv", legacy_key="arxiv_search_resume"
    ) == legacy
    new = {"query_signature": "new", "cursor": "50", "done": False}
    store_provider_resume(
        checkpoint,
        provider_id="arxiv",
        resume=new,
        legacy_key="arxiv_search_resume",
    )
    assert checkpoint["search_resume"]["arxiv"] == new
    assert checkpoint["arxiv_search_resume"] == new
    assert search_query_signature({"b": 2, "a": 1}) == search_query_signature(
        {"a": 1, "b": 2}
    )


@respx.mock
async def test_crossref_provider_maps_metadata_relations_and_cursor():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    work = {
        "DOI": "10.1000/ARTICLE",
        "title": ["A Crossref Paper"],
        "author": [{"given": "Alice", "family": "Smith"}],
        "container-title": ["Journal of Tests"],
        "published-online": {"date-parts": [[2025, 6, 2]]},
        "URL": "https://doi.org/10.1000/article",
        "relation": {
            "is-preprint-of": [{"id-type": "doi", "id": "10.1000/JOURNAL"}]
        },
    }
    detail = respx.get("https://api.crossref.org/works/10.1000%2FARTICLE").mock(
        return_value=httpx.Response(200, json={"message": work})
    )
    search = respx.get("https://api.crossref.org/works").mock(
        return_value=httpx.Response(
            200, json={"message": {"items": [work], "next-cursor": "next page"}}
        )
    )
    provider = CrossrefProvider(CrossrefClient(redis=redis, mailto="test@example.org"))

    records = await provider.get_metadata([Identifier("doi", "10.1000/ARTICLE")])
    assert records[0].identifier("doi") == "10.1000/ARTICLE"
    assert records[0].authors == ({"name": "Alice Smith"},)
    assert records[0].relations[0].target == Identifier("doi", "10.1000/JOURNAL")
    page = await provider.search(SearchQuery(text="crossref", limit=10))
    assert page.next_cursor == "next page"
    assert page.records[0].venue == "Journal of Tests"

    unsupported_page = await provider.search(
        SearchQuery(text="crossref", sort="citation", date_from=records[0].published_at)
    )
    assert unsupported_page.unsupported_filters == ("date_from", "sort")

    await provider.get_metadata([Identifier("doi", "10.1000/ARTICLE")])
    assert detail.call_count == 1
    assert search.call_count == 2
    await redis.aclose()


async def test_openalex_and_semantic_scholar_adapters_expose_capabilities():
    class OpenAlexStub:
        async def search_works(self, query, *, limit):  # noqa: ANN001
            return [
                {
                    "openalex_id": "https://openalex.org/W1",
                    "title": query,
                    "doi": "10.1/x",
                    "authors": [{"name": "Ada"}],
                    "affiliations": ["Lab"],
                }
            ]

    class S2Stub:
        async def search_papers(self, query, *, limit):  # noqa: ANN001
            return [
                {
                    "paperId": "s2-id",
                    "title": query,
                    "externalIds": {"ArXiv": "2401.00001", "DOI": "10.2/y"},
                    "authors": [{"name": "Grace"}],
                }
            ]

    openalex = OpenAlexProvider(OpenAlexStub())  # type: ignore[arg-type]
    s2 = SemanticScholarProvider(S2Stub())  # type: ignore[arg-type]
    assert openalex.capabilities.search and openalex.capabilities.metadata
    assert s2.capabilities.references and s2.capabilities.citations
    assert (await openalex.search(SearchQuery(text="openalex"))).records[0].identifier(
        "openalex"
    ) == "W1"
    assert (await s2.search(SearchQuery(text="s2"))).records[0].identifier("arxiv") == (
        "2401.00001"
    )


async def test_provider_record_import_reuses_global_identifier(client):
    from app.services.literature.contracts import ProviderRecord

    first = ProviderRecord(
        source="openalex",
        source_record_id="W1",
        title="Canonical Import",
        identifiers=(Identifier("doi", "10.1000/ABC"), Identifier("openalex", "W1")),
        authors=({"name": "Alice"},),
        year=2025,
    )
    same = ProviderRecord(
        source="crossref",
        source_record_id="10.1000/abc",
        title="Canonical Import from Crossref",
        identifiers=(Identifier("doi", "https://doi.org/10.1000/abc"),),
    )
    async with get_sessionmaker()() as session:
        created = await paper_import.import_provider_record(session, record=first)
        await session.commit()
        reused = await paper_import.import_provider_record(session, record=same)
        await session.commit()
        assert created.created is True
        assert reused.created is False
        assert reused.paper.id == created.paper.id
        identifiers = (
            await session.execute(
                select(PaperIdentifier).where(PaperIdentifier.paper_id == created.paper.id)
            )
        ).scalars().all()
        assert {(row.namespace, row.normalized_value) for row in identifiers} == {
            ("doi", "10.1000/abc"),
            ("openalex", "w1"),
        }
        assert await session.get(Paper, uuid.UUID(str(created.paper.id))) is not None


async def test_doi_resolution_falls_back_to_crossref(monkeypatch):
    class MissingOpenAlex:
        async def get_by_doi(self, doi):  # noqa: ANN001
            return None

    class CrossrefStub:
        async def get_work(self, doi):  # noqa: ANN001
            return {
                "DOI": doi,
                "title": ["Crossref Fallback"],
                "author": [{"given": "Ada", "family": "Lovelace"}],
                "issued": {"date-parts": [[2024]]},
                "container-title": ["Fallback Journal"],
            }

    monkeypatch.setattr(paper_import, "get_openalex_client", lambda: MissingOpenAlex())
    monkeypatch.setattr(paper_import, "get_crossref_client", lambda: CrossrefStub())
    fields = await paper_import.resolve_import_input(ImportInput("doi", "10.1000/FALLBACK"))
    assert fields["source"] == "crossref"
    assert fields["doi"] == "10.1000/FALLBACK"
    assert fields["title"] == "Crossref Fallback"
