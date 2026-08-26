"""Smoke tests for the Polaris extension batch/archive contract."""

import hashlib
import uuid

import pymupdf
import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import new_paper
from tests.conftest import register_and_login


def _pdf_bytes(title: str = "test") -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), title)
    data = document.tobytes()
    document.close()
    return data


async def _target(email: str, *, name: str = "batch library", papers: int = 1):
    async with get_sessionmaker()() as session:
        owner = (
            await session.execute(
                select(__import__("app.models.user", fromlist=["User"]).User).where(
                    __import__("app.models.user", fromlist=["User"]).User.email == email
                )
            )
        ).scalar_one()
        library = DirectionLibrary(
            name=name,
            status="active",
            is_public=False,
            submitted_by=owner.id,
            created_by=owner.id,
        )
        session.add(library)
        await session.flush()
        paper_rows = []
        for index in range(papers):
            paper = new_paper(
                dedup_key=f"doi:10.5555/batch-{uuid.uuid4().hex}",
                title=f"Batch paper {index}",
                doi=f"10.5555/batch-{uuid.uuid4().hex}",
                url="https://publisher.example/article",
            )
            # The dedup key is the stable identity used by the asset contract.
            paper.doi = paper.dedup_key.removeprefix("doi:")
            session.add(paper)
            await session.flush()
            session.add(LibraryPaper(library_id=library.id, paper_id=paper.id, status="included"))
            paper_rows.append(paper)
        await session.commit()
        return str(library.id), [str(paper.id) for paper in paper_rows]


@pytest.mark.asyncio
async def test_batch_creates_one_batch_and_rotates_key(client):
    token = await register_and_login(client, email="batch-owner@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    library_id, paper_ids = await _target("batch-owner@example.com", papers=2)

    first = await client.post("/api/me/download-api-key", headers=auth)
    second = await client.post("/api/me/download-api-key", headers=auth)
    assert first.status_code == second.status_code == 200
    assert first.json()["api_key"] != second.json()["api_key"]
    assert (
        await client.get(
            "/api/download-client/me", headers={"X-Polaris-API-Key": first.json()["api_key"]}
        )
    ).status_code == 401
    assert (
        await client.get(
            "/api/download-client/me", headers={"X-Polaris-API-Key": second.json()["api_key"]}
        )
    ).status_code == 200

    created = await client.post(
        "/api/download-batches",
        headers=auth,
        json={
            "targets": [{"library_id": library_id, "paper_id": paper_id} for paper_id in paper_ids]
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["item_count"] == 2
    assert len(created.json()["items"]) == 2
    assert {item["status"] for item in created.json()["items"]} == {"queued"}
    batches = (await client.get("/api/download-batches", headers=auth)).json()
    batch = next(value for value in batches if value["id"] == created.json()["id"])
    assert len(batch["items"]) == 2
    assert {item["status"] for item in batch["items"]} == {"queued"}


@pytest.mark.asyncio
async def test_batch_claim_upload_is_bound_and_idempotent(client, queue_stub):
    token = await register_and_login(client, email="archive-owner@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    library_id, paper_ids = await _target("archive-owner@example.com")
    created = await client.post(
        "/api/download-batches",
        headers=auth,
        json={"targets": [{"library_id": library_id, "paper_id": paper_ids[0]}]},
    )
    assert created.status_code == 200
    api_key = (await client.post("/api/me/download-api-key", headers=auth)).json()["api_key"]
    key_headers = {"X-Polaris-API-Key": api_key}
    item = (await client.post("/api/download-client/items/claim", headers=key_headers)).json()
    data = _pdf_bytes("archive target")
    cached = await client.post(
        f"/api/download-client/items/{item['id']}/cache",
        headers={**key_headers, "X-Polaris-Lease-Token": item["lease_token"]},
        json={"sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data)},
    )
    assert cached.status_code == 200
    response = await client.post(
        f"/api/download-client/items/{item['id']}/pdf",
        headers={
            **key_headers,
            "X-Polaris-Lease-Token": item["lease_token"],
            "X-Polaris-PDF-SHA256": hashlib.sha256(data).hexdigest(),
        },
        files={"pdf": ("paper.pdf", data, "application/pdf")},
    )
    assert response.status_code == 201, response.text
    assert any(job[0] == "parse_paper_content_task" for job in queue_stub.jobs)
    again = await client.post(
        f"/api/download-client/items/{item['id']}/pdf",
        headers={**key_headers, "X-Polaris-Lease-Token": item["lease_token"]},
        files={"pdf": ("paper.pdf", data, "application/pdf")},
    )
    assert again.status_code == 201
    assert again.json()["asset_id"] == response.json()["asset_id"]


@pytest.mark.asyncio
async def test_cached_target_is_skipped_without_losing_item(client):
    token = await register_and_login(client, email="cached-owner@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    library_id, paper_ids = await _target("cached-owner@example.com")
    async with get_sessionmaker()() as session:
        from app.models.library_direction import DirectionLibrary
        from app.models.paper import Paper
        from app.models.user import User
        from app.services.paper_assets import create_or_reuse_asset

        user = (
            await session.execute(select(User).where(User.email == "cached-owner@example.com"))
        ).scalar_one()
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        paper = await session.get(Paper, uuid.UUID(paper_ids[0]))
        await create_or_reuse_asset(
            session,
            paper=paper,
            library=library,
            content=_pdf_bytes("cached"),
            user=user,
            source="manual",
            identity_key=paper.dedup_key,
            identity_status="verified",
        )
        await session.commit()
    created = await client.post(
        "/api/download-batches",
        headers=auth,
        json={"targets": [{"library_id": library_id, "paper_id": paper_ids[0]}]},
    )
    assert created.status_code == 200
    assert created.json()["status"] == "completed"
    batch = next(
        value
        for value in (await client.get("/api/download-batches", headers=auth)).json()
        if value["id"] == created.json()["id"]
    )
    assert batch["items"][0]["status"] == "skipped"
    assert batch["items"][0]["result"]["reason"] == "already_cached"


@pytest.mark.asyncio
async def test_extension_api_key_can_create_batch_and_receives_item_status(client):
    token = await register_and_login(client, email="extension-batch-owner@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    library_id, paper_ids = await _target("extension-batch-owner@example.com", papers=2)
    api_key = (await client.post("/api/me/download-api-key", headers=auth)).json()["api_key"]
    created = await client.post(
        "/api/download-batches",
        headers={"X-Polaris-API-Key": api_key},
        json={
            "targets": [
                {"library_id": library_id, "paper_id": paper_id} for paper_id in paper_ids
            ]
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["item_count"] == 2
    assert [item["paper_id"] for item in created.json()["items"]] == paper_ids


@pytest.mark.asyncio
async def test_direct_archive_rejects_wrong_identity(client, queue_stub):
    token = await register_and_login(client, email="identity-owner@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    library_id, paper_ids = await _target("identity-owner@example.com")
    api_key = (await client.post("/api/me/download-api-key", headers=auth)).json()["api_key"]
    metadata = {
        "library_id": library_id,
        "paper_id": paper_ids[0],
        "nonce": "identity-test-nonce-001",
        "doi": "10.5555/wrong",
        "title": "Batch paper 0",
    }
    response = await client.post(
        "/api/download-client/archive",
        headers={"X-Polaris-API-Key": api_key},
        data={"metadata": __import__("json").dumps(metadata)},
        files={"pdf": ("paper.pdf", _pdf_bytes("wrong"), "application/pdf")},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "DOWNLOAD_ARCHIVE_IDENTITY_MISMATCH"
