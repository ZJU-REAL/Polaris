"""Issue #455: versioned parser lifecycle and vector-state persistence."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.embedding_space import EmbeddingSpace
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import new_paper
from app.models.paper_assets import AssetGrant
from app.models.paper_content import (
    PaperContentChunkVector,
    PaperContentVersion,
    PaperContentVersionVector,
)
from app.models.user import User
from app.services.evidence import (
    current_fulltext_evidence,
    keyword_search_current_fulltext,
)
from app.services.paper_assets import create_or_reuse_asset
from app.services.paper_content import (
    ContentParseError,
    create_content_version,
    current_content_version,
    parse_content_version,
    vectorize_content_version,
)
from tests.test_paper_assets import _pdf_bytes, _user


async def _setup(client):
    _headers, user_id = await _user(client, f"content-{uuid.uuid4().hex}@example.com")
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        library = DirectionLibrary(
            name=f"content-{uuid.uuid4().hex[:8]}",
            statement="content test",
            status="active",
            submitted_by=user_id,
            created_by=user_id,
        )
        paper = new_paper(title="Content paper", doi="10.1234/content")
        session.add_all([library, paper])
        await session.flush()
        session.add(LibraryPaper(library_id=library.id, paper_id=paper.id, status="included"))
        asset = await create_or_reuse_asset(
            session,
            paper=paper,
            library=library,
            content=_pdf_bytes("full text page"),
            user=user,
            source="upload",
            sharing_scope="private",
        )
        await session.commit()
        return user, library, paper, asset


@pytest.mark.asyncio
async def test_mineru_failure_falls_back_to_pymupdf_and_persists_version(app, client):
    _user_row, _library, paper, asset = await _setup(client)
    async with get_sessionmaker()() as session:
        version = await create_content_version(session, asset=asset)

        async def failing_mineru(_path):
            raise RuntimeError("mineru unavailable")

        parsed = await parse_content_version(session, version=version, mineru_parser=failing_mineru)
        assert parsed.status == "ready_fallback"
        assert parsed.parser == "pymupdf"
        assert parsed.page_count == 1
        assert parsed.chunk_count == 1
        assert parsed.attempt == 1
        assert await current_content_version(session, paper_id=paper.id)
        context = await current_fulltext_evidence(
            session, paper_id=paper.id, query="full text", limit=4
        )
        assert context is not None
        assert context["parser"] == "pymupdf"
        assert context["chunks"][0]["text"]
        assert context["chunks"][0]["evidence"][0]["href"].startswith(
            f"/papers/{paper.id}/read?evidence=1"
        )


@pytest.mark.asyncio
async def test_reparse_creates_new_current_version_without_mutating_old(app, client):
    _user_row, _library, paper, asset = await _setup(client)
    async with get_sessionmaker()() as session:
        first = await create_content_version(session, asset=asset, parser="pymupdf")
        await parse_content_version(session, version=first, mineru_parser=None)
        second = await create_content_version(session, asset=asset)
        assert second.version_no == first.version_no + 1
        await parse_content_version(session, version=second, mineru_parser=None)
        await session.refresh(first)
        assert first.is_current is False
        assert second.is_current is True
        saved = await session.scalar(
            select(PaperContentVersion).where(PaperContentVersion.id == first.id)
        )
        assert saved is not None and saved.is_current is False
        context = await current_fulltext_evidence(session, paper_id=paper.id)
        assert context is not None
        assert context["version_id"] == str(second.id)
        assert context["chunks"][0]["evidence"]


@pytest.mark.asyncio
async def test_failed_reparse_keeps_previous_current_version(app, client):
    _user_row, _library, paper, asset = await _setup(client)
    async with get_sessionmaker()() as session:
        first = await create_content_version(session, asset=asset, parser="pymupdf")
        await parse_content_version(session, version=first, mineru_parser=None)
        second = await create_content_version(session, asset=asset)

        async def failing_mineru(_path):
            raise RuntimeError("cloud timeout")

        with pytest.raises(ContentParseError, match="MINERU_FAILED"):
            await parse_content_version(
                session,
                version=second,
                mineru_parser=failing_mineru,
                allow_fallback=False,
            )
        current = await current_content_version(session, paper_id=paper.id)
        assert current is not None
        assert current.id == first.id
        assert second.is_current is False


@pytest.mark.asyncio
async def test_mineru_artifacts_are_persisted_with_content_version(app, client):
    _user_row, _library, _paper, asset = await _setup(client)
    async with get_sessionmaker()() as session:
        version = await create_content_version(session, asset=asset)

        async def fake_mineru(_path):
            return {
                "parser": "mineru",
                "parser_version": "cloud-test",
                "markdown": "# Full text\n\n![Figure](images/figure.png)",
                "text": "Full text",
                "pages": 1,
                "chunks": [{"text": "Full text", "page_start": 1, "page_end": 1}],
                "markdown_path": "paper.md",
                "artifacts": [{"path": "images/figure.png", "kind": "image", "content": b"png"}],
                "manifest": {"pages": 1, "images": ["images/figure.png"]},
            }

        parsed = await parse_content_version(session, version=version, mineru_parser=fake_mineru)
        version_dir = Path(parsed.markdown_key).parent
        assert (version_dir / "paper.md").read_text(encoding="utf-8").startswith("# Full")
        assert (version_dir / "images" / "figure.png").read_bytes() == b"png"


@pytest.mark.asyncio
async def test_persistence_failure_marks_attempt_failed_and_keeps_previous_current(
    app, client, monkeypatch
):
    _user_row, _library, paper, asset = await _setup(client)
    async with get_sessionmaker()() as session:
        first = await create_content_version(session, asset=asset, parser="pymupdf")
        await parse_content_version(session, version=first, mineru_parser=None)
        second = await create_content_version(session, asset=asset)

        async def fake_mineru(_path):
            return {
                "parser": "mineru",
                "markdown": "# Parsed\n\nFull text",
                "text": "Full text",
                "pages": 1,
                "chunks": [{"text": "Full text", "page_start": 1, "page_end": 1}],
            }

        async def fail_anchor_persistence(*_args, **_kwargs):
            raise RuntimeError("database write failed")

        monkeypatch.setattr(
            "app.services.paper_content.persist_chunk_anchors", fail_anchor_persistence
        )
        with pytest.raises(ContentParseError, match="CONTENT_PERSIST_FAILED"):
            await parse_content_version(
                session, version=second, mineru_parser=fake_mineru, allow_fallback=False
            )
        await session.refresh(first)
        failed = await session.get(PaperContentVersion, second.id)
        current = await current_content_version(session, paper_id=paper.id)

    assert first.is_current is True
    assert current is not None and current.id == first.id
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_code == "CONTENT_PERSIST_FAILED"


@pytest.mark.asyncio
async def test_current_fulltext_search_enforces_asset_grant(app, client):
    _user_row, library, _paper, asset = await _setup(client)
    async with get_sessionmaker()() as session:
        version = await create_content_version(session, asset=asset)
        await parse_content_version(session, version=version, mineru_parser=None)
        hits = await keyword_search_current_fulltext(
            session,
            library_ids=[library.id],
            query="full text",
            limit=4,
        )
        assert len(hits) == 1
        assert hits[0].paper_id == asset.paper_id

        grant = await session.scalar(
            select(AssetGrant).where(
                AssetGrant.asset_id == asset.id,
                AssetGrant.library_id == library.id,
            )
        )
        assert grant is not None
        grant.can_read = False
        await session.flush()
        denied = await keyword_search_current_fulltext(
            session,
            library_ids=[library.id],
            query="full text",
            limit=4,
        )
        assert denied == []


@pytest.mark.asyncio
async def test_mineru_failure_without_fallback_is_terminal(app, client):
    _user_row, _library, _paper, asset = await _setup(client)
    async with get_sessionmaker()() as session:
        version = await create_content_version(session, asset=asset)

        async def failing_mineru(_path):
            raise RuntimeError("mineru unavailable")

        with pytest.raises(ContentParseError, match="MINERU_FAILED"):
            await parse_content_version(
                session,
                version=version,
                mineru_parser=failing_mineru,
                allow_fallback=False,
            )
        assert version.status == "failed"
        assert version.error_code == "MINERU_FAILED"


@pytest.mark.asyncio
async def test_vectorize_persists_document_and_chunk_vectors(app, client, monkeypatch):
    _user_row, library, _paper, asset = await _setup(client)
    space = EmbeddingSpace(model="test-embedding", dim=3)

    async def fake_embed_documents(session, texts, **_kwargs):
        return [[float(index), 0.5, 1.0] for index, _ in enumerate(texts)], space

    monkeypatch.setattr("app.services.embedding.embed_documents", fake_embed_documents)
    async with get_sessionmaker()() as session:
        version = await create_content_version(session, asset=asset)
        await parse_content_version(session, version=version, mineru_parser=None)
        await vectorize_content_version(session, version=version, library_id=library.id)
        await session.refresh(version)
        assert version.status == "vector_ready"
        assert version.document_vector_state == "ready"
        assert version.chunk_vector_state == "ready"
        assert (
            await session.scalar(
                select(PaperContentVersionVector).where(
                    PaperContentVersionVector.content_version_id == version.id
                )
            )
        ) is not None
        assert (await session.scalar(select(PaperContentChunkVector))) is not None
