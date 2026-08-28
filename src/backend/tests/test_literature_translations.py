"""Issue #512: idempotent, model-versioned discovery metadata translations."""

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary
from app.models.literature_discovery import LiteratureHitTranslation, LiteratureSearchHit
from app.models.user import User
from app.services.literature.translations import (
    execute_translation,
    request_translation,
    source_hash,
)
from tests.conftest import register_and_login


class TranslationRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def model_name(self, stage, user_id=None):
        assert stage == "translation"
        return "translation-model-v1"

    async def complete(self, stage, messages, **kwargs):
        self.calls.append((stage, messages, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(content=response, model="translation-model-v1")


async def _run_and_hits(client, count=2):
    token = await register_and_login(client, email=f"translation-{uuid.uuid4().hex}@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    library = await client.post(
        "/api/libraries",
        json={"name": "Translation library", "statement": "Structural dynamics"},
        headers=headers,
    )
    assert library.status_code == 201, library.text
    library_id = library.json()["id"]
    run = await client.post(
        f"/api/libraries/{library_id}/literature/runs",
        json={
            "topic": "structural dynamics",
            "source_config": {"sources": []},
        },
        headers=headers,
    )
    assert run.status_code == 201, run.text
    run_id = uuid.UUID(run.json()["id"])
    hit_ids = []
    async with get_sessionmaker()() as session:
        for index in range(count):
            hit = LiteratureSearchHit(
                run_id=run_id,
                source="crossref",
                dedup_key=f"doi:10.1000/{index}",
                title=f"Impact response {index}",
                abstract=f"Full abstract {index}",
                scores={"reasons": [f"Relevant mechanism {index}"]},
            )
            session.add(hit)
            await session.flush()
            hit_ids.append(hit.id)
        await session.commit()
    return headers, library_id, str(run_id), hit_ids


@pytest.mark.asyncio
async def test_translation_cache_is_idempotent_and_preserves_source_metadata(client):
    _, _, _, hit_ids = await _run_and_hits(client, count=1)
    router = TranslationRouter(
        [
            json.dumps(
                {
                    "title": "冲击响应",
                    "abstract": "完整摘要",
                    "inclusion_rationale": ["机制相关"],
                },
                ensure_ascii=False,
            )
        ]
    )
    async with get_sessionmaker()() as session:
        hit = await session.get(LiteratureSearchHit, hit_ids[0])
        before = (hit.title, hit.abstract, dict(hit.scores), source_hash(hit))
        row, enqueue = await request_translation(
            session,
            hit=hit,
            target_language="zh-CN",
            model="translation-model-v1",
        )
        again, enqueue_again = await request_translation(
            session,
            hit=hit,
            target_language="zh-cn",
            model="translation-model-v1",
        )
        assert row.id == again.id
        assert enqueue is True
        assert enqueue_again is False

        translated = await execute_translation(
            session,
            translation_id=row.id,
            llm=router,
        )
        hit = await session.get(LiteratureSearchHit, hit.id)

    assert translated.status == "ready"
    assert translated.translated_fields["title"] == "冲击响应"
    assert (hit.title, hit.abstract, dict(hit.scores), source_hash(hit)) == before


@pytest.mark.asyncio
async def test_translation_keeps_requesting_user_for_worker_model_resolution(client):
    _, _, _, hit_ids = await _run_and_hits(client, count=1)
    requester_email = f"translation-requester-{uuid.uuid4().hex}@example.com"
    await register_and_login(client, email=requester_email)
    router = TranslationRouter(
        [
            json.dumps(
                {
                    "title": "冲击响应",
                    "abstract": "完整摘要",
                    "inclusion_rationale": ["机制相关"],
                },
                ensure_ascii=False,
            )
        ]
    )
    async with get_sessionmaker()() as session:
        requester_id = await session.scalar(select(User.id).where(User.email == requester_email))
        assert requester_id is not None
        hit = await session.get(LiteratureSearchHit, hit_ids[0])
        row, _ = await request_translation(
            session,
            hit=hit,
            target_language="zh-CN",
            model="translation-model-v1",
            requested_by=requester_id,
        )
        assert row.requested_by == requester_id
        translated = await execute_translation(
            session,
            translation_id=row.id,
            llm=router,
            user_id=row.requested_by,
        )

    assert translated.status == "ready"
    assert router.calls[0][2]["user_id"] == requester_id


@pytest.mark.asyncio
async def test_failed_translation_is_retryable_without_failing_another_item(client):
    _, _, _, hit_ids = await _run_and_hits(client, count=2)
    router = TranslationRouter(
        [
            "not-json",
            json.dumps(
                {
                    "title": "第二篇",
                    "abstract": "第二篇摘要",
                    "inclusion_rationale": ["相关"],
                },
                ensure_ascii=False,
            ),
        ]
    )
    async with get_sessionmaker()() as session:
        rows = []
        for hit_id in hit_ids:
            hit = await session.get(LiteratureSearchHit, hit_id)
            row, _ = await request_translation(
                session,
                hit=hit,
                target_language="zh-CN",
                model="translation-model-v1",
            )
            rows.append(row)
        failed = await execute_translation(session, translation_id=rows[0].id, llm=router)
        ready = await execute_translation(session, translation_id=rows[1].id, llm=router)
        assert failed.status == "failed"
        assert failed.error_code == "TRANSLATION_OUTPUT_INVALID"
        hit = await session.get(LiteratureSearchHit, hit_ids[0])
        retried, should_enqueue = await request_translation(
            session,
            hit=hit,
            target_language="zh-CN",
            model="translation-model-v1",
        )

    assert retried.status == "queued"
    assert retried.error_code is None
    assert ready.status == "ready"
    assert retried.id == rows[0].id
    assert should_enqueue is True


async def test_translation_api_reports_progress_and_deduplicates_requests(
    client, queue_stub, monkeypatch
):
    headers, library_id, run_id, hit_ids = await _run_and_hits(client, count=2)
    router = TranslationRouter([])
    monkeypatch.setattr("app.api.literature_discovery.get_llm_router", lambda: router)

    response = await client.post(
        f"/api/libraries/{library_id}/literature/runs/{run_id}/translations",
        json={"hit_ids": [str(hit_id) for hit_id in hit_ids], "target_language": "zh-CN"},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    assert [item["status"] for item in response.json()] == ["queued", "queued"]
    assert len(queue_stub.jobs) == 2

    response = await client.post(
        f"/api/libraries/{library_id}/literature/runs/{run_id}/hits/{hit_ids[0]}/translation",
        json={"target_language": "zh-cn"},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    assert len(queue_stub.jobs) == 2

    response = await client.get(
        f"/api/libraries/{library_id}/literature/runs/{run_id}/hits/{hit_ids[0]}/translation",
        params={"target_language": "zh-CN"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "queued"

    async with get_sessionmaker()() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(LiteratureHitTranslation)
            .where(
                LiteratureHitTranslation.hit_id.in_(hit_ids)
            )
        )
        assert count == 2


async def test_public_library_reader_cannot_create_translation_jobs(
    client, queue_stub, monkeypatch
):
    _, library_id, run_id, hit_ids = await _run_and_hits(client, count=1)
    reader_token = await register_and_login(
        client, email=f"translation-reader-{uuid.uuid4().hex}@example.com"
    )
    reader_headers = {"Authorization": f"Bearer {reader_token}"}
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(library_id))
        assert library is not None
        library.is_public = True
        await session.commit()

    monkeypatch.setattr(
        "app.api.literature_discovery.get_llm_router",
        lambda: TranslationRouter([]),
    )
    response = await client.post(
        f"/api/libraries/{library_id}/literature/runs/{run_id}/hits/{hit_ids[0]}/translation",
        json={"target_language": "zh-CN"},
        headers=reader_headers,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "LIBRARY_DISCOVERY_FORBIDDEN"
    assert queue_stub.jobs == []


async def test_batch_translation_continues_after_one_queue_dispatch_failure(
    client, queue_stub, monkeypatch
):
    headers, library_id, run_id, hit_ids = await _run_and_hits(client, count=2)
    monkeypatch.setattr(
        "app.api.literature_discovery.get_llm_router",
        lambda: TranslationRouter([]),
    )
    enqueue = queue_stub.enqueue
    attempts = 0

    async def fail_first_dispatch(func, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("queue unavailable")
        await enqueue(func, *args, **kwargs)

    queue_stub.enqueue = fail_first_dispatch
    response = await client.post(
        f"/api/libraries/{library_id}/literature/runs/{run_id}/translations",
        json={"hit_ids": [str(hit_id) for hit_id in hit_ids], "target_language": "zh-CN"},
        headers=headers,
    )

    assert response.status_code == 202, response.text
    assert [item["status"] for item in response.json()] == ["failed", "queued"]
    assert response.json()[0]["error_code"] == "QUEUE_DISPATCH_FAILED"
    assert len(queue_stub.jobs) == 1
