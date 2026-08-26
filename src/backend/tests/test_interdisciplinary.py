"""Smoke tests for the interdisciplinary project/profile/library contract."""

import pytest

from tests.conftest import register_and_login


@pytest.mark.asyncio
async def test_scope_confirmation_creates_one_dedicated_library(client):
    token = await register_and_login(client, email="interdisciplinary-owner@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/api/projects",
        headers=auth,
        json={
            "name": "Impact-aware segmentation",
            "statement": "Study structural response with vision-based measurements.",
            "research_mode": "interdisciplinary",
        },
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    scope = {
        "research_scope": (
            "Use segmentation observations to explain structural response under dynamic impact."
        ),
        "core_questions": ["Which visual measurements are mechanically meaningful?"],
        "primary_domain": "Structural engineering",
        "related_domains": ["Computer vision", "Data-driven mechanics"],
        "evidence_boundary": (
            "Only claims supported by the selected library and validated experiments."
        ),
        "validation_conditions": ["Compare predicted and measured displacement fields."],
        "user_questions": [],
    }
    saved = await client.put(
        f"/api/projects/{project_id}/interdisciplinary/scope", headers=auth, json=scope
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "draft"
    confirmed = await client.post(
        f"/api/projects/{project_id}/interdisciplinary/scope/confirm", headers=auth
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["profile"]["status"] == "confirmed"
    library_id = confirmed.json()["library_id"]

    repeated = await client.post(
        f"/api/projects/{project_id}/interdisciplinary/scope/confirm", headers=auth
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["library_id"] == library_id

    libraries = await client.get(f"/api/projects/{project_id}/source-libraries", headers=auth)
    assert libraries.status_code == 200, libraries.text
    row = next(item for item in libraries.json() if item["id"] == library_id)
    assert row["library_kind"] == "interdisciplinary"
    assert row["interdisciplinary_domains"] == [
        "Structural engineering",
        "Computer vision",
        "Data-driven mechanics",
    ]


@pytest.mark.asyncio
async def test_interdisciplinary_scope_is_owner_managed(client):
    owner_token = await register_and_login(client, email="interdisciplinary-owner-2@example.com")
    other_token = await register_and_login(client, email="interdisciplinary-viewer@example.com")
    project = await client.post(
        "/api/projects",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"name": "Private cross domain topic", "research_mode": "interdisciplinary"},
    )
    project_id = project.json()["id"]
    response = await client.put(
        f"/api/projects/{project_id}/interdisciplinary/scope",
        headers={"Authorization": f"Bearer {other_token}"},
        json={
            "research_scope": "An unauthorized profile must not be writable.",
            "core_questions": ["Q"],
            "primary_domain": "Engineering",
            "related_domains": ["Computing"],
        },
    )
    assert response.status_code in {403, 404}
