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
    assert [a["source"] for a in run["source_attempts"]] == ["openalex", "semantic"]

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
            scores={"relevance": 0.9, "novelty": 0.4, "impact": 8},
        )
        session.add(hit)
        await session.commit()
    response = await client.get(
        f"/api/libraries/{library_id}/literature/runs/{run_id}/hits?sort=relevance&q=structural",
        headers=owner,
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["title"] == "Impact response"


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
