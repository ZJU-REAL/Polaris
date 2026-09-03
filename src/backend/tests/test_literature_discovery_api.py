"""Issue #453: library-scoped discovery API and authorization."""

import uuid

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary
from app.models.literature_discovery import LiteratureSearchHit
from tests.conftest import register_and_login


async def _headers(client, email: str) -> dict[str, str]:
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def _personal_library(client, headers: dict[str, str]) -> str:
    response = await client.post(
        "/api/libraries",
        json={"name": "Discovery API", "statement": "Search API permissions"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_owner_can_create_inspect_filter_and_cancel_run(client):
    await _headers(client, "discovery-admin@example.com")
    owner = await _headers(client, "discovery-owner@example.com")
    library_id = await _personal_library(client, owner)

    response = await client.post(
        f"/api/libraries/{library_id}/literature/runs",
        json={
            "requested_count": 12,
            "candidate_budget": 40,
            "start_year": 2016,
            "end_year": 2026,
            "topic": "structural impact response",
            "source_config": {"sources": ["openalex", "semantic"]},
            "query_plan": {"sources": ["crossref"]},
        },
        headers=owner,
    )
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["requested_count"] == 12
    assert run["candidate_budget"] == 40
    assert run["progress"]["requested_count"] == 12
    assert run["progress"]["candidate_budget"] == 40
    assert run["progress"]["returned_count"] == 0
    assert [a["source"] for a in run["source_attempts"]] == ["openalex", "semantic"]
    assert all(attempt["requested_count"] is None for attempt in run["source_attempts"])

    run_id = run["id"]
    response = await client.get(
        f"/api/libraries/{library_id}/literature/runs/{run_id}", headers=owner
    )
    assert response.status_code == 200
    response = await client.post(
        f"/api/libraries/{library_id}/literature/runs/{run_id}/cancel", headers=owner
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    async with get_sessionmaker()() as session:
        hit = LiteratureSearchHit(
            run_id=uuid.UUID(run_id),
            source="openalex",
            dedup_key="doi:10.1/example",
            title="Impact response",
            abstract="A structural impact response",
            year=2024,
            scores={"relevance": 0.9, "novelty": 0.4, "impact": 8},
        )
        older = LiteratureSearchHit(
            run_id=uuid.UUID(run_id),
            source="semantic",
            dedup_key="doi:10.1/older",
            title="Older mechanics study",
            abstract="Historical mechanics evidence",
            year=2012,
            scores={"relevance": 0.7, "novelty": 0.2, "impact": 6},
        )
        session.add_all([hit, older])
        await session.commit()
    response = await client.get(
        f"/api/libraries/{library_id}/literature/runs/{run_id}/hits?sort=relevance&q=structural",
        headers=owner,
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["title"] == "Impact response"

    response = await client.get(
        f"/api/libraries/{library_id}/literature/runs/{run_id}/hits?year_from=2020&year_to=2026",
        headers=owner,
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["year"] == 2024

    response = await client.get(
        f"/api/libraries/{library_id}/literature/runs/{run_id}/hits?year_from=2026&year_to=2020",
        headers=owner,
    )
    assert response.status_code == 422
    assert "YEAR_RANGE_INVALID" in response.text


async def test_candidate_budget_cannot_be_smaller_than_requested_result_count(client):
    owner = await _headers(client, "discovery-budget-owner@example.com")
    library_id = await _personal_library(client, owner)

    response = await client.post(
        f"/api/libraries/{library_id}/literature/runs",
        json={
            "requested_count": 50,
            "candidate_budget": 20,
            "topic": "structural impact response",
            "source_config": {"sources": ["openalex"]},
        },
        headers=owner,
    )

    assert response.status_code == 201, response.text
    assert response.json()["requested_count"] == 50
    assert response.json()["candidate_budget"] == 50


async def test_run_snapshot_inherits_library_keywords_exclusions_and_rubric(client):
    owner = await _headers(client, "discovery-library-config@example.com")
    library_id = await _personal_library(client, owner)
    rubric = [{"name": "mechanism evidence", "description": "Prefer validated mechanics"}]
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        assert library is not None
        library.definition = {
            "statement": "Impact response of structural members",
            "keywords": {
                "include": ["impact response", "damage mechanics"],
                "exclude": ["review"],
            },
            "rubric": rubric,
        }
        await session.commit()

    response = await client.post(
        f"/api/libraries/{library_id}/literature/runs",
        json={
            "topic": "structural impact response",
            "source_config": {"sources": ["openalex"]},
        },
        headers=owner,
    )

    assert response.status_code == 201, response.text
    source_config = response.json()["source_config"]
    assert source_config["keywords"] == ["impact response", "damage mechanics"]
    assert source_config["excluded_keywords"] == ["review"]
    assert source_config["score_rubric"] == rubric


async def test_personal_library_is_isolated_and_public_library_is_read_only(client):
    await _headers(client, "visibility-admin@example.com")
    owner = await _headers(client, "visibility-owner@example.com")
    stranger = await _headers(client, "visibility-stranger@example.com")
    library_id = await _personal_library(client, owner)

    response = await client.get(f"/api/libraries/{library_id}/literature/runs", headers=stranger)
    assert response.status_code == 404
    response = await client.post(
        f"/api/libraries/{library_id}/literature/runs",
        json={"topic": "forbidden"},
        headers=stranger,
    )
    assert response.status_code == 404

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        library.is_public = True
        await session.commit()
    response = await client.get(f"/api/libraries/{library_id}/literature/runs", headers=stranger)
    assert response.status_code == 200
    response = await client.post(
        f"/api/libraries/{library_id}/literature/runs",
        json={"topic": "read-only public access"},
        headers=stranger,
    )
    assert response.status_code == 403


async def test_owner_configures_and_triggers_incremental_schedule(client, queue_stub):
    owner = await _headers(client, "discovery-schedule-owner@example.com")
    library_id = await _personal_library(client, owner)
    payload = {
        "enabled": True,
        "timezone": "UTC",
        "hour": 3,
        "minute": 45,
        "requested_count": 50,
        "candidate_budget": 200,
        "start_year": 2016,
        "end_year": 2026,
    }

    response = await client.put(
        f"/api/libraries/{library_id}/literature/schedule",
        json=payload,
        headers=owner,
    )
    assert response.status_code == 200, response.text
    schedule = response.json()
    assert schedule["config_version"] == 1
    assert schedule["requested_count"] == 50
    assert schedule["start_year"] == 2016
    assert schedule["next_run_at"] is not None

    response = await client.post(
        f"/api/libraries/{library_id}/literature/schedule/run",
        headers=owner,
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["trigger"] == "scheduled"
    assert run["schedule_version"] == 1
    assert run["requested_count"] == 50
    assert run["start_year"] == 2016
    assert run["source_config"]["incremental_discovery"]["schedule_version"] == 1
    assert queue_stub.jobs == [("run_literature_discovery", (run["id"],), {})]

    response = await client.post(
        f"/api/libraries/{library_id}/literature/schedule/run",
        headers=owner,
    )
    assert response.status_code == 409
    assert "LITERATURE_SCHEDULE_RUN_ACTIVE" in response.text

    response = await client.get(
        f"/api/libraries/{library_id}/literature/schedule",
        headers=owner,
    )
    assert response.status_code == 200
    assert response.json()["last_run_id"] == run["id"]
    assert response.json()["last_enqueued_at"] is not None


async def test_public_schedule_is_read_only_for_non_creators(client):
    owner = await _headers(client, "public-schedule-owner@example.com")
    stranger = await _headers(client, "public-schedule-reader@example.com")
    library_id = await _personal_library(client, owner)
    response = await client.put(
        f"/api/libraries/{library_id}/literature/schedule",
        json={"enabled": False, "timezone": "UTC"},
        headers=owner,
    )
    assert response.status_code == 200, response.text
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        assert library is not None
        library.is_public = True
        await session.commit()

    response = await client.get(
        f"/api/libraries/{library_id}/literature/schedule", headers=stranger
    )
    assert response.status_code == 200
    response = await client.put(
        f"/api/libraries/{library_id}/literature/schedule",
        json={"enabled": True, "timezone": "UTC"},
        headers=stranger,
    )
    assert response.status_code == 403
    response = await client.delete(
        f"/api/libraries/{library_id}/literature/schedule", headers=stranger
    )
    assert response.status_code == 403


async def test_schedule_rejects_invalid_timezone_and_year_window(client):
    owner = await _headers(client, "invalid-schedule-owner@example.com")
    library_id = await _personal_library(client, owner)
    response = await client.put(
        f"/api/libraries/{library_id}/literature/schedule",
        json={"enabled": True, "timezone": "Not/A_Real_Zone"},
        headers=owner,
    )
    assert response.status_code == 422
    assert "INVALID_TIMEZONE" in response.text

    response = await client.put(
        f"/api/libraries/{library_id}/literature/schedule",
        json={"start_year": 2026, "end_year": 2016},
        headers=owner,
    )
    assert response.status_code == 422
