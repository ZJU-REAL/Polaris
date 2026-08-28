"""Issue #510: provider-isolated, versioned venue metric enrichment."""

import httpx
import pytest
from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models.literature_discovery import LiteratureVenueMetricCache
from app.services.literature.venue_metrics import (
    EasyScholarVenueMetricProvider,
    OpenAlexVenueMetricProvider,
    VenueIdentity,
    VenueMetricProviderError,
    VenueMetricResult,
    VenueMetricService,
    metric_impact_score,
    normalize_venue,
    venue_identity,
)


class FakeProvider:
    name = "fake"

    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    async def lookup(self, identity):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_venue_identity_rejects_placeholders_and_prefers_issn():
    assert normalize_venue(" Unknown journal ") is None
    assert venue_identity({"venue": "Unknown journal"}) is None

    identity = venue_identity(
        {"venue": "Journal of Tests", "metadata": {"issn_l": "1234-567X"}}
    )
    assert identity == VenueIdentity(
        key="issn:1234-567X",
        name="Journal of Tests",
        issn_l="1234-567X",
        issns=("1234-567X",),
    )

    crossref = venue_identity(
        {
            "venue": "Journal of Tests",
            "metadata": {
                "ISSN": ["9876-5432"],
                "issn-type": [{"value": "1234-567X", "type": "electronic"}],
            },
        }
    )
    assert crossref is not None
    assert crossref.issns == ("9876-5432", "1234-567X")


@pytest.mark.asyncio
async def test_metric_service_caches_resolved_snapshot_without_mutating_metadata(client):
    provider = FakeProvider(
        VenueMetricResult(
            provider="fake",
            venue_name="Journal of Tests",
            issn_l="1234-567X",
            metrics={"impact_factor": 8.0, "jcr_quartile": "Q1"},
        )
    )
    service = VenueMetricService([provider], ttl_days=30)
    candidate = {
        "title": "A paper",
        "venue": "Journal of Tests",
        "metadata": {"source_field": "unchanged"},
    }
    async with get_sessionmaker()() as session:
        first = await service.enrich_candidates(session, [candidate])
        await session.commit()
        second = await service.enrich_candidates(session, [candidate])
        cache_count = await session.scalar(
            select(func.count()).select_from(LiteratureVenueMetricCache)
        )

    assert provider.calls == 1
    assert cache_count == 1
    assert first[0]["metadata"] == {"source_field": "unchanged"}
    assert first[0]["venue_metric_snapshot"]["version"] == "venue-metrics-v1"
    assert first[0]["venue_metric_snapshot"]["metrics"]["impact_factor"] == 8.0
    assert first[0]["impact_score"] == second[0]["impact_score"]


@pytest.mark.asyncio
async def test_metric_provider_failure_is_visible_but_candidate_remains_usable(client):
    provider = FakeProvider(error=VenueMetricProviderError("fake", "TIMEOUT"))
    service = VenueMetricService([provider])
    async with get_sessionmaker()() as session:
        result = await service.enrich_candidates(
            session, [{"title": "Candidate", "venue": "Journal of Tests"}]
        )

    assert result[0]["title"] == "Candidate"
    assert result[0]["venue_metric_snapshot"]["metrics"] is None
    assert result[0]["venue_metric_snapshot"]["provider_errors"] == {"fake": "TIMEOUT"}
    assert "impact_score" not in result[0]


def test_verified_metrics_map_only_to_bounded_impact_score():
    score = metric_impact_score(
        {"impact_factor": 12.5, "h_index": 150, "jcr_quartile": "Q1"}
    )
    assert score is not None and 0 < score <= 1
    assert metric_impact_score({"provider_error": "TIMEOUT"}) is None


@pytest.mark.asyncio
async def test_openalex_provider_requires_exact_title_match():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/S1",
                        "type": "journal",
                        "display_name": "Journal of Tests",
                        "summary_stats": {"2yr_mean_citedness": 3.5, "h_index": 42},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAlexVenueMetricProvider(client)
        result = await provider.lookup(
            VenueIdentity("name:journaloftests", "Journal of Tests", None, ())
        )

    assert result is not None
    assert result.metrics["two_year_mean_citedness"] == 3.5
    assert result.metrics["h_index"] == 42


@pytest.mark.asyncio
async def test_easyscholar_provider_maps_rank_fields_without_storing_secret():
    observed_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_url
        observed_url = str(request.url)
        return httpx.Response(
            200,
            request=request,
            json={
                "code": 200,
                "data": {
                    "officialRank": {
                        "all": {"sciif": "6.2", "sci": "Q1", "sciBase": "2区"}
                    }
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = EasyScholarVenueMetricProvider(
            client, keys=["SECRET_SENTINEL"], base_url="https://metrics.example.test/rank"
        )
        result = await provider.lookup(
            VenueIdentity("name:journaloftests", "Journal of Tests", None, ())
        )

    assert "SECRET_SENTINEL" in observed_url
    assert result is not None
    assert result.metrics == {"impact_factor": 6.2, "jcr_quartile": "Q1", "cas_base_zone": 2}
    assert "SECRET_SENTINEL" not in repr(result)
