"""Content-addressed PDF storage and explicit library grants."""

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.library_direction import DirectionLibrary
from app.models.paper import Paper
from app.models.paper_assets import AssetGrant, PaperAsset, PdfBlob
from app.models.user import User
from app.services import libraries as libraries_service

MAX_PDF_BYTES = 100 * 1024 * 1024
SHARING_SCOPES = frozenset({"private", "library", "public"})


class AssetError(RuntimeError):
    """Expected asset validation or authorization failure."""


class AssetNotFoundError(AssetError):
    pass


class AssetPermissionError(AssetError):
    pass


class AssetAlreadyExistsError(AssetError):
    pass


def _blob_root() -> Path:
    root = Path(get_settings().data_dir) / "pdf-blobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def blob_storage_path(sha256: str) -> Path:
    """Resolve a validated digest to its content-addressed path."""
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise AssetError("invalid PDF blob digest")
    path = _blob_root() / sha256[:2] / f"{sha256}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _validate_pdf(content: bytes) -> None:
    if not content or len(content) > MAX_PDF_BYTES:
        raise AssetError("PDF is empty or exceeds the size limit")
    if not content.startswith(b"%PDF-"):
        raise AssetError("file header is not PDF")
    try:
        import pymupdf

        with pymupdf.open(stream=content, filetype="pdf") as document:
            if document.needs_pass or document.page_count < 1:
                raise AssetError("encrypted or empty PDF is not supported")
    except AssetError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AssetError("PDF cannot be read") from exc


def _write_blob(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def can_manage_library(
    session: AsyncSession, *, library: DirectionLibrary, user: User
) -> bool:
    return await libraries_service.can_manage_library(session, user=user, library=library)


def _paper_identity_keys(paper: Paper) -> set[str]:
    keys = {paper.dedup_key.lower()} if paper.dedup_key else set()
    if paper.doi:
        keys.add(f"doi:{paper.doi.strip().lower().removeprefix('https://doi.org/')}")
    if paper.arxiv_id:
        keys.add(f"arxiv:{paper.arxiv_id.strip().lower()}")
    return keys


async def create_or_reuse_asset(
    session: AsyncSession,
    *,
    paper: Paper,
    library: DirectionLibrary,
    content: bytes,
    user: User,
    source: str,
    source_locator: str | None = None,
    identity_key: str | None = None,
    identity_status: str = "verified",
    sharing_scope: str = "private",
) -> PaperAsset:
    """Persist one immutable PDF and grant the target library access.

    The blob is globally deduplicated, while the asset and grant remain explicit
    records. Existing private assets are never implicitly reused by another library.
    """
    if not await can_manage_library(session, library=library, user=user):
        raise AssetPermissionError("LIBRARY_ASSET_MANAGE_FORBIDDEN")
    if sharing_scope not in SHARING_SCOPES:
        raise AssetError("invalid sharing scope")
    if source not in {"oa", "upload", "extension", "arxiv", "manual", "unknown"}:
        raise AssetError("invalid asset source")
    normalized_identity = identity_key.strip().lower() if identity_key else None
    if (
        identity_status == "verified"
        and normalized_identity is not None
        and normalized_identity not in _paper_identity_keys(paper)
    ):
        raise AssetError("PDF identity does not match the target paper")
    await asyncio.to_thread(_validate_pdf, content)
    digest = hashlib.sha256(content).hexdigest()
    blob = await session.scalar(select(PdfBlob).where(PdfBlob.sha256 == digest))
    if blob is None:
        blob = PdfBlob(
            sha256=digest,
            byte_size=len(content),
            storage_key=f"pdf-blobs/{digest[:2]}/{digest}.pdf",
            content_type="application/pdf",
            state="ready",
        )
        session.add(blob)
        await session.flush()
    path = blob_storage_path(blob.sha256)
    if not path.exists() or path.stat().st_size != len(content):
        await asyncio.to_thread(_write_blob, path, content)

    asset = await session.scalar(
        select(PaperAsset).where(
            PaperAsset.paper_id == paper.id,
            PaperAsset.blob_id == blob.id,
            PaperAsset.source == source,
        )
    )
    if asset is None:
        asset = PaperAsset(
            paper_id=paper.id,
            blob_id=blob.id,
            source=source,
            source_locator=source_locator,
            identity_key=normalized_identity,
            identity_status=identity_status,
            sharing_scope=sharing_scope,
            state="ready",
            is_preferred=True,
            metadata_snapshot={"sha256": digest, "byte_size": len(content)},
        )
        session.add(asset)
        await session.flush()
    else:
        if asset.sharing_scope == "private" and asset.sharing_scope != sharing_scope:
            raise AssetAlreadyExistsError("asset sharing scope cannot be widened in place")

    existing_grant = await session.scalar(
        select(AssetGrant).where(
            AssetGrant.asset_id == asset.id,
            AssetGrant.library_id == library.id,
        )
    )
    if existing_grant is None:
        session.add(
            AssetGrant(
                asset_id=asset.id,
                library_id=library.id,
                status="active",
                can_read=True,
                can_process=True,
                granted_by=user.id,
            )
        )
    elif existing_grant.status != "active":
        existing_grant.status = "active"
        existing_grant.can_read = True
        existing_grant.can_process = True
        existing_grant.revoked_by = None
        existing_grant.granted_by = user.id
    # Keep the legacy reader path working while the asset APIs migrate callers.
    if not paper.pdf_path:
        paper.pdf_path = str(path)
    await session.flush()
    return asset


async def grant_existing_asset(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    target_library: DirectionLibrary,
    user: User,
) -> AssetGrant:
    """Grant an existing asset only when its sharing policy allows reuse."""
    if not await can_manage_library(session, library=target_library, user=user):
        raise AssetPermissionError("LIBRARY_ASSET_MANAGE_FORBIDDEN")
    asset = await session.get(PaperAsset, asset_id)
    if asset is None:
        raise AssetNotFoundError("ASSET_NOT_FOUND")
    if asset.sharing_scope != "public":
        existing_target_grant = await session.scalar(
            select(AssetGrant).where(
                AssetGrant.asset_id == asset.id,
                AssetGrant.library_id == target_library.id,
                AssetGrant.status == "active",
            )
        )
        if existing_target_grant is None:
            raise AssetPermissionError("ASSET_NOT_SHAREABLE_ACROSS_LIBRARIES")
    grant = await session.scalar(
        select(AssetGrant).where(
            AssetGrant.asset_id == asset.id,
            AssetGrant.library_id == target_library.id,
        )
    )
    if grant is None:
        grant = AssetGrant(
            asset_id=asset.id,
            library_id=target_library.id,
            status="active",
            can_read=True,
            can_process=True,
            granted_by=user.id,
        )
        session.add(grant)
    else:
        grant.status = "active"
        grant.can_read = True
        grant.can_process = True
        grant.revoked_by = None
        grant.granted_by = user.id
    await session.flush()
    return grant


async def grant_public_asset_for_paper(
    session: AsyncSession,
    *,
    paper_id: uuid.UUID,
    target_library: DirectionLibrary,
    user: User,
) -> AssetGrant:
    """Reuse the preferred public asset for a DOI/identifier-resolved paper."""
    asset = await session.scalar(
        select(PaperAsset)
        .join(AssetGrant, AssetGrant.asset_id == PaperAsset.id)
        .where(
            PaperAsset.paper_id == paper_id,
            PaperAsset.sharing_scope == "public",
            PaperAsset.state == "ready",
            AssetGrant.status == "active",
            AssetGrant.can_read.is_(True),
        )
        .order_by(PaperAsset.is_preferred.desc(), PaperAsset.created_at.asc())
    )
    if asset is None:
        raise AssetNotFoundError("PUBLIC_ASSET_NOT_FOUND")
    return await grant_existing_asset(
        session, asset_id=asset.id, target_library=target_library, user=user
    )


async def list_assets(
    session: AsyncSession, *, paper_id: uuid.UUID, library_id: uuid.UUID
) -> list[tuple[PaperAsset, PdfBlob, AssetGrant]]:
    rows = await session.execute(
        select(PaperAsset, PdfBlob, AssetGrant)
        .join(PdfBlob, PdfBlob.id == PaperAsset.blob_id)
        .join(AssetGrant, AssetGrant.asset_id == PaperAsset.id)
        .where(
            PaperAsset.paper_id == paper_id,
            AssetGrant.library_id == library_id,
            AssetGrant.status == "active",
        )
        .order_by(PaperAsset.is_preferred.desc(), PaperAsset.created_at.desc())
    )
    return list(rows.all())


async def readable_asset(
    session: AsyncSession, *, asset_id: uuid.UUID, library_id: uuid.UUID
) -> tuple[PaperAsset, PdfBlob] | None:
    row = await session.execute(
        select(PaperAsset, PdfBlob)
        .join(PdfBlob, PdfBlob.id == PaperAsset.blob_id)
        .join(AssetGrant, AssetGrant.asset_id == PaperAsset.id)
        .where(
            PaperAsset.id == asset_id,
            AssetGrant.library_id == library_id,
            AssetGrant.status == "active",
            AssetGrant.can_read.is_(True),
            PdfBlob.state == "ready",
        )
    )
    return row.first()


def storage_path_for_blob(blob: PdfBlob) -> Path:
    expected = blob_storage_path(blob.sha256)
    if blob.storage_key != f"pdf-blobs/{blob.sha256[:2]}/{blob.sha256}.pdf":
        raise AssetError("blob storage key mismatch")
    return expected
