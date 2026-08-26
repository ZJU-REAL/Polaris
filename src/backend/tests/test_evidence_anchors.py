"""证据锚点生成、版本回退和迁移回归。"""

import uuid
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from app.core.db import get_sessionmaker
from app.models.evidence import PaperEvidenceAnchor
from app.models.paper import new_paper
from app.models.paper_assets import PaperAsset, PdfBlob
from app.models.paper_content import PaperContentChunk, PaperContentVersion
from app.services.evidence import (
    build_chunk_anchor_payloads,
    content_revision,
    normalize_evidence_text,
    persist_chunk_anchors,
    resolve_evidence_anchor,
    split_sentences,
)


def test_normalization_and_sentence_split_are_deterministic() -> None:
    text = "A hyphen-\nated result is stable. 第二句有效。"
    assert normalize_evidence_text(text) == "a hyphenated result is stable. 第二句有效。"
    assert split_sentences(text) == ["A hyphen-\nated result is stable.", "第二句有效。"]


def test_payloads_include_sentence_paragraph_and_chunk_anchors() -> None:
    paper_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    payloads = build_chunk_anchor_payloads(
        paper_id=paper_id,
        chunk_id=chunk_id,
        seq=3,
        text="First sentence. Second sentence.\n\nA new paragraph.",
        source="fulltext",
        page_start=4,
        page_end=5,
        rects=[{"x0": 0.1, "y0": 0.2, "x1": 0.5, "y1": 0.3}],
    )
    assert {payload.anchor_type for payload in payloads} == {"chunk", "paragraph", "sentence"}
    full_text = "First sentence. Second sentence.\n\nA new paragraph."
    assert all(
        payload.content_revision
        == content_revision(
            payload.quoted_text if payload.anchor_type == "chunk" else full_text
        )
        for payload in payloads
    )
    assert all(payload.locator["page_start"] == 4 for payload in payloads)


@pytest.mark.asyncio
async def test_persist_is_idempotent_and_keeps_reparse_revision(app) -> None:
    chunk_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        paper = new_paper(title="Evidence test paper")
        session.add(paper)
        await session.flush()
        blob = PdfBlob(
            sha256="a" * 64,
            byte_size=1,
            storage_key=f"pdf-blobs/aa/{'a' * 64}.pdf",
            content_type="application/pdf",
            state="ready",
        )
        session.add(blob)
        await session.flush()
        asset = PaperAsset(paper_id=paper.id, blob_id=blob.id, source="upload", state="ready")
        session.add(asset)
        await session.flush()
        version = PaperContentVersion(
            paper_id=paper.id,
            asset_id=asset.id,
            version_no=1,
            parser="pymupdf",
            status="ready_fallback",
            is_current=True,
        )
        session.add(version)
        await session.flush()
        chunk = PaperContentChunk(
            id=chunk_id,
            content_version_id=version.id,
            seq=0,
            text="Original sentence.",
        )
        session.add(chunk)
        await session.flush()
        first = await persist_chunk_anchors(session, paper_id=paper.id, chunks=[chunk])
        second = await persist_chunk_anchors(session, paper_id=paper.id, chunks=[chunk])
        chunk.text = "Reparsed sentence."
        await session.flush()
        third = await persist_chunk_anchors(session, paper_id=paper.id, chunks=[chunk])
        await session.commit()
        assert first == 3 and second == 0 and third == 3
        rows = (
            await session.execute(
                __import__("sqlalchemy").select(PaperEvidenceAnchor).where(
                    PaperEvidenceAnchor.paper_id == paper.id
                )
            )
        ).scalars().all()
        assert len(rows) == 6


@pytest.mark.asyncio
async def test_resolve_falls_back_to_chunk_then_paper(app) -> None:
    chunk_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        paper = new_paper(title="Evidence fallback paper")
        session.add(paper)
        await session.flush()
        blob = PdfBlob(
            sha256="b" * 64,
            byte_size=1,
            storage_key=f"pdf-blobs/bb/{'b' * 64}.pdf",
            content_type="application/pdf",
            state="ready",
        )
        session.add(blob)
        await session.flush()
        asset = PaperAsset(paper_id=paper.id, blob_id=blob.id, source="upload", state="ready")
        session.add(asset)
        await session.flush()
        version = PaperContentVersion(
            paper_id=paper.id,
            asset_id=asset.id,
            version_no=1,
            parser="pymupdf",
            status="ready_fallback",
            is_current=True,
        )
        session.add(version)
        await session.flush()
        chunk = PaperContentChunk(
            id=chunk_id,
            content_version_id=version.id,
            seq=0,
            text="A sentence that will change.",
        )
        session.add(chunk)
        await session.flush()
        await persist_chunk_anchors(session, paper_id=paper.id, chunks=[chunk])
        await session.commit()
        anchor = (
            await session.execute(
                __import__("sqlalchemy").select(PaperEvidenceAnchor).where(
                    PaperEvidenceAnchor.paper_id == paper.id,
                    PaperEvidenceAnchor.anchor_type == "sentence",
                )
            )
        ).scalars().first()
        assert anchor is not None
        chunk.text = "A completely unrelated replacement."
        await session.flush()
        result = await resolve_evidence_anchor(session, anchor, current_chunks=[chunk])
        assert result.status == "chunk"
        assert result.anchor_type == "chunk"
        assert result.href.endswith(f"evidence={anchor.id}")


def test_migration_upgrade_and_downgrade_roundtrip(tmp_path: Path) -> None:
    cfg = Config()
    backend_dir = Path(__file__).resolve().parent.parent
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{tmp_path / 'evidence.db'}")
    command.upgrade(cfg, "head")
    import sqlite3

    with sqlite3.connect(tmp_path / "evidence.db") as connection:
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list('paper_evidence_anchors')"
        ).fetchall()
    assert any(row[2] == "paper_content_chunks" for row in foreign_keys)
    command.downgrade(cfg, "-1")
