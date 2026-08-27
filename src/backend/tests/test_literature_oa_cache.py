"""Issue #475: OA cache is durable and remains separate from promotion."""

import uuid

import pymupdf
import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.literature_discovery import (
    LiteratureOaAttempt,
    LiteratureOaCache,
    LiteratureSearchHit,
    LiteratureSearchRun,
)
from app.services.literature import oa_cache
from tests.test_literature_discovery_runtime import _create_run


def _pdf_bytes() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "OA cache test")
    data = document.tobytes()
    document.close()
    return data


@pytest.mark.asyncio
async def test_oa_cache_persists_verified_blob_and_attempt(client, monkeypatch):
    run_id, _headers, _library_id = await _create_run(
        client, source_config={"sources": ["openalex"]}
    )
    async with get_sessionmaker()() as session:
        hit = LiteratureSearchHit(
            run_id=run_id,
            source="openalex",
            dedup_key=f"doi:10.1234/{uuid.uuid4().hex}",
            title="OA cache test",
            doi="10.1234/oa-cache",
            pdf_url="https://papers.example.test/test.pdf",
        )
        session.add(hit)
        await session.flush()

        async def fake_download(_url):
            return _pdf_bytes(), "https://papers.example.test/test.pdf", 200

        async def fake_write(path, content):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        monkeypatch.setattr(oa_cache, "_download", fake_download)
        monkeypatch.setattr(oa_cache, "_write_blob", fake_write)
        cache = await oa_cache.cache_hit_pdf(session, hit)
        await session.commit()

        assert cache.status == "ready"
        assert cache.blob_id is not None
        assert cache.sha256 is not None
        assert await session.scalar(
            select(LiteratureOaAttempt).where(LiteratureOaAttempt.cache_id == cache.id)
        )
        assert await session.scalar(
            select(LiteratureSearchRun.id).where(LiteratureSearchRun.id == run_id)
        )
        assert await session.scalar(
            select(LiteratureOaCache.id).where(LiteratureOaCache.id == cache.id)
        )


@pytest.mark.asyncio
async def test_oa_cache_without_pdf_url_is_not_promoted(client):
    run_id, _headers, _library_id = await _create_run(
        client, source_config={"sources": ["openalex"]}
    )
    async with get_sessionmaker()() as session:
        hit = LiteratureSearchHit(
            run_id=run_id,
            source="openalex",
            dedup_key=f"title:{uuid.uuid4().hex}",
            title="No OA file",
        )
        session.add(hit)
        await session.flush()
        cache = await oa_cache.cache_hit_pdf(session, hit)
        assert cache.status == "unavailable"
        assert cache.error_code == "OA_PDF_NOT_FOUND"
        assert (
            await session.scalar(
                select(LiteratureOaAttempt.id).where(
                    LiteratureOaAttempt.cache_id == cache.id
                )
            )
            is None
        )
