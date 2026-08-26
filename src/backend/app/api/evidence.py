"""Library-scoped evidence resolution for AI citation navigation."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.evidence import PaperEvidenceAnchor
from app.models.user import User
from app.schemas.evidence import EvidenceResolution
from app.services import libraries as libraries_service
from app.services import paper_assets as asset_service
from app.services import paper_content as content_service
from app.services import papers as papers_service
from app.services.evidence import resolve_evidence_anchor

router = APIRouter(tags=["evidence"])


@router.get(
    "/libraries/{library_id}/papers/{paper_id}/evidence/{anchor_id}",
    response_model=EvidenceResolution,
)
async def resolve_library_evidence(
    library_id: uuid.UUID,
    paper_id: uuid.UUID,
    anchor_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> EvidenceResolution:
    library = await libraries_service.get_library(session, library_id)
    if library is None or not libraries_service.library_visible_to(library, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    paper = await papers_service.get_library_paper_view(
        session, library_id=library_id, project_id=None, paper_id=paper_id, with_concepts=False
    )
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    anchor = await session.scalar(
        select(PaperEvidenceAnchor).where(
            PaperEvidenceAnchor.id == anchor_id,
            PaperEvidenceAnchor.paper_id == paper_id,
        )
    )
    if anchor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="EVIDENCE_NOT_FOUND")
    version = await content_service.current_content_version(session, paper_id=paper_id)
    chunks = (
        await content_service.list_content_chunks(session, version_id=version.id)
        if version is not None
        else []
    )
    result = await resolve_evidence_anchor(session, anchor, current_chunks=chunks)
    if version is None or await asset_service.readable_asset(
        session, asset_id=version.asset_id, library_id=library_id
    ) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="EVIDENCE_ASSET_NOT_FOUND")
    return result
