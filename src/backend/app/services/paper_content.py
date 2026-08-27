"""MinerU-first, retryable parsing lifecycle for PDF assets."""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

import pymupdf
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.paper_assets import PaperAsset, PdfBlob
from app.models.paper_content import (
    PaperContentChunk,
    PaperContentChunkVector,
    PaperContentVersion,
    PaperContentVersionVector,
)
from app.services.evidence import persist_chunk_anchors
from app.services.paper_assets import storage_path_for_blob

logger = logging.getLogger(__name__)

ParserResult = dict[str, Any]
MineruParser = Callable[[Path], Awaitable[ParserResult]]
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ContentParseError(RuntimeError):
    """A parse attempt failed after the fallback policy was applied."""


def _clean_text(value: str) -> str:
    return _CONTROL_RE.sub("", (value or "").replace("\r\n", "\n").replace("\r", "\n")).strip()


def _content_root() -> Path:
    root = Path(get_settings().data_dir) / "paper-content"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _version_dir(version_id: uuid.UUID) -> Path:
    path = _content_root() / str(version_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _output_path(root: Path, name: object, *, fallback: str | None = None) -> Path | None:
    """Resolve an archive-relative output path below the immutable version directory."""
    relative = PurePosixPath(str(name or "").replace("\\", "/"))
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or ":" in relative.parts[0]
    ):
        if fallback is None:
            return None
        relative = PurePosixPath(fallback)
    path = root.joinpath(*relative.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _pymupdf_parse_sync(pdf_path: Path) -> ParserResult:
    pages: list[str] = []
    chunks: list[dict[str, Any]] = []
    with pymupdf.open(pdf_path) as document:
        for page_no, page in enumerate(document, start=1):
            text = _clean_text(page.get_text("text"))
            pages.append(text)
            if text:
                chunks.append(
                    {
                        "text": text,
                        "page_start": page_no,
                        "page_end": page_no,
                        "rects": [],
                        "section_path": [],
                        "anchor_meta": {"parser": "pymupdf", "page": page_no},
                    }
                )
    text = _clean_text("\n\n".join(pages))
    if not text:
        raise ContentParseError("PDF_TEXT_EMPTY")
    markdown = "\n\n".join(
        f"## Page {i}\n\n{page}"
        for i, page in enumerate(pages, start=1)
        if page
    )
    return {
        "parser": "pymupdf",
        "parser_version": getattr(pymupdf, "VersionBind", None) or "unknown",
        "markdown": markdown,
        "text": text,
        "pages": len(pages),
        "chunks": chunks,
        "manifest": {"pages": len(pages), "images": [], "tables": []},
    }


async def create_content_version(
    session: AsyncSession,
    *,
    asset: PaperAsset,
    parser: str = "mineru",
    parser_version: str | None = None,
) -> PaperContentVersion:
    """Create an immutable attempt without replacing usable content prematurely."""
    current = await session.scalar(
        select(PaperContentVersion.id).where(
            PaperContentVersion.paper_id == asset.paper_id,
            PaperContentVersion.is_current.is_(True),
        )
    )
    latest = await session.scalar(
        select(func.max(PaperContentVersion.version_no)).where(
            PaperContentVersion.paper_id == asset.paper_id
        )
    )
    version = PaperContentVersion(
        paper_id=asset.paper_id,
        asset_id=asset.id,
        version_no=int(latest or 0) + 1,
        parser=parser,
        parser_version=parser_version,
        status="queued",
        is_current=current is None,
    )
    session.add(version)
    await session.flush()
    return version


async def parse_content_version(
    session: AsyncSession,
    *,
    version: PaperContentVersion,
    mineru_parser: MineruParser | None = None,
    allow_fallback: bool = True,
) -> PaperContentVersion:
    """Run one version, persisting status before and after each expensive stage."""
    asset = await session.get(PaperAsset, version.asset_id)
    if asset is None:
        raise ContentParseError("PAPER_ASSET_MISSING")
    blob = await session.get(PdfBlob, asset.blob_id)
    if blob is None:
        raise ContentParseError("PDF_BLOB_MISSING")
    pdf_path = storage_path_for_blob(blob)
    if not pdf_path.is_file():
        raise ContentParseError("PDF_FILE_MISSING")

    version.attempt += 1
    version.status = "mineru_uploading" if version.parser == "mineru" else "parsing"
    version.error_code = None
    version.error_detail = None
    await session.commit()

    result: ParserResult
    parser_name = version.parser
    try:
        if mineru_parser is None:
            if version.parser == "mineru":
                from app.services.mineru import MineruCloudParser

                mineru_parser = MineruCloudParser()
            else:
                raise ContentParseError("MINERU_ADAPTER_NOT_CONFIGURED")
        if hasattr(mineru_parser, "parse"):
            async def update_status(value: str) -> None:
                version.status = value
                await session.commit()

            try:
                result = await mineru_parser.parse(pdf_path, on_status=update_status)  # type: ignore[attr-defined]
            finally:
                close = getattr(mineru_parser, "aclose", None)
                if close is not None:
                    await close()
        else:
            result = await mineru_parser(pdf_path)
        parser_name = "mineru"
    except Exception as exc:  # noqa: BLE001 - fallback is an explicit policy
        if not allow_fallback:
            version.status = "failed"
            version.error_code = "MINERU_FAILED"
            version.error_detail = f"{type(exc).__name__}: {exc}"[:4000]
            await session.commit()
            raise ContentParseError("MINERU_FAILED") from exc
        version.status = "fallback_parsing"
        version.metadata_snapshot = {
            **(version.metadata_snapshot or {}),
            "fallback_reason": f"{type(exc).__name__}: {exc}"[:1000],
        }
        await session.commit()
        try:
            result = await asyncio.to_thread(_pymupdf_parse_sync, pdf_path)
            parser_name = "pymupdf"
        except Exception as fallback_exc:  # noqa: BLE001
            version.status = "failed"
            version.error_code = "PYMUPDF_FAILED"
            version.error_detail = f"{type(fallback_exc).__name__}: {fallback_exc}"[:4000]
            await session.commit()
            raise ContentParseError("PYMUPDF_FAILED") from fallback_exc

    text = _clean_text(str(result.get("text") or ""))
    markdown = _clean_text(str(result.get("markdown") or text))
    chunks = result.get("chunks") or []
    if not text or not chunks:
        version.status = "failed"
        version.error_code = "PARSED_CONTENT_EMPTY"
        version.error_detail = "parser returned no text or chunks"
        await session.commit()
        raise ContentParseError("PARSED_CONTENT_EMPTY")

    directory = _version_dir(version.id)
    text_path = directory / "content.txt"
    markdown_path = _output_path(
        directory, result.get("markdown_path"), fallback="content.md"
    )
    if markdown_path is None:
        raise ContentParseError("MARKDOWN_PATH_INVALID")
    manifest_path = directory / "manifest.json"
    text_path.write_text(text, encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    for artifact in result.get("artifacts") or []:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("content"), bytes):
            continue
        artifact_path = _output_path(directory, artifact.get("path"))
        if artifact_path is not None:
            artifact_path.write_bytes(artifact["content"])
    manifest_path.write_text(
        json.dumps(result.get("manifest") or {}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    await session.execute(
        delete(PaperContentChunk).where(
            PaperContentChunk.content_version_id == version.id
        )
    )
    persisted_chunks: list[PaperContentChunk] = []
    for seq, item in enumerate(chunks):
        chunk_text = _clean_text(str(item.get("text") or ""))
        if not chunk_text:
            continue
        chunk = PaperContentChunk(
            content_version_id=version.id,
            seq=seq,
            text=chunk_text,
            page_start=item.get("page_start"),
            page_end=item.get("page_end"),
            rects=item.get("rects") or [],
            section_path=item.get("section_path") or [],
            anchor_meta=item.get("anchor_meta") or {},
        )
        session.add(chunk)
        persisted_chunks.append(chunk)
    await session.flush()  # chunk.id 供锚点引用
    await persist_chunk_anchors(
        session,
        paper_id=version.paper_id,
        chunks=persisted_chunks,
        source=parser_name,
    )
    version.parser = parser_name
    version.parser_version = str(
        result.get("parser_version") or version.parser_version or "unknown"
    )
    version.status = "ready" if parser_name == "mineru" else "ready_fallback"
    version.markdown_key = str(markdown_path)
    version.text_key = str(text_path)
    version.manifest_key = str(manifest_path)
    version.page_count = int(result.get("pages") or 0)
    version.chunk_count = len([item for item in chunks if str(item.get("text") or "").strip()])
    version.chunk_vector_state = "pending"
    version.document_vector_state = "pending"
    await session.execute(
        PaperContentVersion.__table__.update()
        .where(
            PaperContentVersion.paper_id == version.paper_id,
            PaperContentVersion.id != version.id,
        )
        .values(is_current=False)
    )
    version.is_current = True
    await session.commit()
    return version


async def current_content_version(
    session: AsyncSession, *, paper_id: uuid.UUID
) -> PaperContentVersion | None:
    return await session.scalar(
        select(PaperContentVersion)
        .where(PaperContentVersion.paper_id == paper_id, PaperContentVersion.is_current.is_(True))
        .order_by(PaperContentVersion.version_no.desc())
    )


async def list_content_chunks(
    session: AsyncSession, *, version_id: uuid.UUID
) -> list[PaperContentChunk]:
    return list(
        (
            await session.execute(
                select(PaperContentChunk)
                .where(PaperContentChunk.content_version_id == version_id)
                .order_by(PaperContentChunk.seq)
            )
        )
        .scalars()
        .all()
    )


async def vectorize_content_version(
    session: AsyncSession,
    *,
    version: PaperContentVersion,
    user_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
) -> PaperContentVersion:
    """Embed parsed full text and every chunk in the active embedding space."""
    if version.status not in {"ready", "ready_fallback", "vector_ready"}:
        raise ContentParseError("CONTENT_NOT_READY")
    chunks = await list_content_chunks(session, version_id=version.id)
    if not chunks or not version.text_key or not Path(version.text_key).is_file():
        raise ContentParseError("CONTENT_TEXT_MISSING")

    from app.services.embedding import embed_documents

    version.document_vector_state = "building"
    version.chunk_vector_state = "building"
    await session.commit()
    try:
        full_text = Path(version.text_key).read_text(encoding="utf-8", errors="ignore")
        document_vectors, space = await embed_documents(
            session,
            [full_text[:12000]],
            user_id=user_id,
            library_id=library_id,
        )
        await session.execute(
            delete(PaperContentVersionVector).where(
                PaperContentVersionVector.content_version_id == version.id,
                PaperContentVersionVector.space == space.key,
            )
        )
        session.add(
            PaperContentVersionVector(
                content_version_id=version.id,
                space=space.key,
                dim=space.dim,
                embedding=document_vectors[0],
                model=space.model,
            )
        )
        for start in range(0, len(chunks), 32):
            batch = chunks[start : start + 32]
            vectors, chunk_space = await embed_documents(
                session,
                [chunk.text[:4000] for chunk in batch],
                user_id=user_id,
                library_id=library_id,
            )
            if chunk_space.key != space.key:
                raise ContentParseError("EMBEDDING_SPACE_CHANGED_DURING_BUILD")
            for chunk, vector in zip(batch, vectors, strict=True):
                await session.execute(
                    delete(PaperContentChunkVector).where(
                        PaperContentChunkVector.chunk_id == chunk.id,
                        PaperContentChunkVector.space == space.key,
                    )
                )
                session.add(
                    PaperContentChunkVector(
                        chunk_id=chunk.id,
                        space=space.key,
                        dim=space.dim,
                        embedding=vector,
                        model=space.model,
                    )
                )
        version.document_vector_state = "ready"
        version.chunk_vector_state = "ready"
        version.status = "vector_ready"
        await session.commit()
    except Exception as exc:
        await session.rollback()
        version = await session.get(PaperContentVersion, version.id)
        if version is not None:
            version.document_vector_state = "failed"
            version.chunk_vector_state = "failed"
            version.error_code = "VECTOR_BUILD_FAILED"
            version.error_detail = f"{type(exc).__name__}: {exc}"[:4000]
            await session.commit()
        raise
    return version
