"""Library-scoped PDF asset endpoints."""

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.schemas.paper_assets import (
    AssetGrantRead,
    AssetReuseRequest,
    PaperAssetPage,
    PaperAssetRead,
)
from app.services import libraries as libraries_service
from app.services import paper_assets as asset_service
from app.services import papers as papers_service

router = APIRouter(tags=["paper-assets"])


async def _library_for_manager(
    session: AsyncSession, library_id: uuid.UUID, user: User
):
    library = await libraries_service.get_library(session, library_id)
    if library is None or not libraries_service.library_visible_to(library, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    if not await libraries_service.can_manage_library(session, user=user, library=library):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="LIBRARY_ASSET_MANAGE_FORBIDDEN")
    return library


async def _paper_in_library(
    session: AsyncSession, library_id: uuid.UUID, paper_id: uuid.UUID, user: User
):
    view = await papers_service.get_library_paper_view(
        session, library_id=library_id, project_id=None, paper_id=paper_id, with_concepts=False
    )
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    return view.paper


def _asset_read(asset, blob) -> PaperAssetRead:
    data = {
        "id": asset.id,
        "paper_id": asset.paper_id,
        "blob_id": asset.blob_id,
        "source": asset.source,
        "source_locator": asset.source_locator,
        "identity_key": asset.identity_key,
        "identity_status": asset.identity_status,
        "sharing_scope": asset.sharing_scope,
        "state": asset.state,
        "is_preferred": asset.is_preferred,
        "byte_size": blob.byte_size,
        "sha256": blob.sha256,
        "created_at": asset.created_at,
        "updated_at": asset.updated_at,
    }
    return PaperAssetRead.model_validate(data)


@router.get(
    "/libraries/{library_id}/papers/{paper_id}/assets", response_model=PaperAssetPage
)
async def list_paper_assets(
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperAssetPage:
    library = await libraries_service.get_library(session, library_id)
    if library is None or not libraries_service.library_visible_to(library, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    await _paper_in_library(session, library_id, paper_id, user)
    rows = await asset_service.list_assets(session, paper_id=paper_id, library_id=library_id)
    return PaperAssetPage(
        items=[
            _asset_read(asset, blob)
            for asset, blob, _grant in rows
        ],
        grants=[AssetGrantRead.model_validate(grant) for _asset, _blob, grant in rows],
    )


@router.post(
    "/libraries/{library_id}/papers/{paper_id}/assets",
    response_model=PaperAssetRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_paper_asset(
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    file: UploadFile = File(...),
    source: str = Form("upload"),
    source_locator: str | None = Form(None),
    identity_key: str | None = Form(None),
    identity_status: str = Form("verified"),
    sharing_scope: str = Form("private"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperAssetRead:
    library = await _library_for_manager(session, library_id, user)
    paper = await _paper_in_library(session, library_id, paper_id, user)
    content = await file.read(asset_service.MAX_PDF_BYTES + 1)
    try:
        asset = await asset_service.create_or_reuse_asset(
            session,
            paper=paper,
            library=library,
            content=content,
            user=user,
            source=source,
            source_locator=source_locator,
            identity_key=identity_key,
            identity_status=identity_status,
            sharing_scope=sharing_scope,
        )
        await session.commit()
    except asset_service.AssetError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    blob = await session.get(asset_service.PdfBlob, asset.blob_id)
    if blob is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="PDF_BLOB_MISSING")
    return _asset_read(asset, blob)


@router.post(
    "/libraries/{library_id}/papers/{paper_id}/assets/reuse",
    response_model=AssetGrantRead,
    status_code=status.HTTP_201_CREATED,
)
async def reuse_paper_asset(
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    body: AssetReuseRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> AssetGrantRead:
    library = await _library_for_manager(session, library_id, user)
    paper = await _paper_in_library(session, library_id, paper_id, user)
    asset = await session.get(asset_service.PaperAsset, body.asset_id)
    if asset is None or asset.paper_id != paper.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ASSET_NOT_FOUND")
    try:
        grant = await asset_service.grant_existing_asset(
            session, asset_id=body.asset_id, target_library=library, user=user
        )
        await session.commit()
    except asset_service.AssetError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return AssetGrantRead.model_validate(grant)


@router.post(
    "/libraries/{library_id}/papers/{paper_id}/assets/reuse-public",
    response_model=AssetGrantRead,
    status_code=status.HTTP_201_CREATED,
)
async def reuse_public_paper_asset(
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> AssetGrantRead:
    """Link the existing public PDF for a DOI/identifier-resolved paper."""
    library = await _library_for_manager(session, library_id, user)
    await _paper_in_library(session, library_id, paper_id, user)
    try:
        grant = await asset_service.grant_public_asset_for_paper(
            session, paper_id=paper_id, target_library=library, user=user
        )
        await session.commit()
    except asset_service.AssetNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except asset_service.AssetPermissionError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return AssetGrantRead.model_validate(grant)


@router.get("/libraries/{library_id}/papers/{paper_id}/assets/{asset_id}/download")
async def download_paper_asset(
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FileResponse:
    library = await libraries_service.get_library(session, library_id)
    if library is None or not libraries_service.library_visible_to(library, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    await _paper_in_library(session, library_id, paper_id, user)
    row = await asset_service.readable_asset(
        session, asset_id=asset_id, library_id=library_id
    )
    if row is None or row[0].paper_id != paper_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ASSET_NOT_FOUND")
    asset, blob = row
    try:
        path = asset_service.storage_path_for_blob(blob)
    except asset_service.AssetError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ASSET_STORAGE_INVALID"
        ) from exc
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ASSET_FILE_MISSING")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{paper_id}-{asset.id}.pdf",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=300"},
    )
