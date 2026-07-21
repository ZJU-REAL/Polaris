"""我发表的论文路由（issue #109）：/me/author-profile + /me/publications。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.queue import TaskQueue, get_task_queue
from app.models.user import User
from app.schemas.publication import (
    AuthorProfileRead,
    AuthorProfileUpdate,
    ManualPublicationCreate,
    PublicationPage,
    PublicationRead,
    SyncEnqueued,
)
from app.services import publications as publications_service
from app.services.paper_import import ParseFailedError

router = APIRouter(tags=["publications"])


@router.get("/me/author-profile", response_model=AuthorProfileRead)
async def get_author_profile(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> AuthorProfileRead:
    profile = await publications_service.get_profile(session, user_id=user.id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROFILE_NOT_FOUND")
    return AuthorProfileRead.model_validate(profile)


@router.put("/me/author-profile", response_model=AuthorProfileRead)
async def put_author_profile(
    body: AuthorProfileUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> AuthorProfileRead:
    profile = await publications_service.upsert_profile(
        session,
        user_id=user.id,
        name_variants=body.name_variants,
        affiliations=body.affiliations,
        openalex_author_id=body.openalex_author_id,
        orcid=body.orcid,
        auto_sync=body.auto_sync,
    )
    return AuthorProfileRead.model_validate(profile)


@router.post(
    "/me/publications/sync", response_model=SyncEnqueued, status_code=status.HTTP_202_ACCEPTED
)
async def trigger_library_match(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
) -> SyncEnqueued:
    """手动触发一次文献库扫描匹配（平时每日 cron 自动跑）。"""
    profile = await publications_service.get_profile(session, user_id=user.id)
    if profile is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="AUTHOR_NOT_BOUND")
    await queue.enqueue("match_user_publications", str(user.id))
    return SyncEnqueued(queued=True)


@router.get("/me/publications", response_model=PublicationPage)
async def list_publications(
    status_filter: str = Query(
        default="confirmed", alias="status", pattern="^(pending|confirmed|rejected)$"
    ),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PublicationPage:
    items, total = await publications_service.list_publications(
        session, user_id=user.id, status=status_filter, page=page, size=size
    )
    counts = await publications_service.status_counts(session, user_id=user.id)
    return PublicationPage(
        items=[PublicationRead.model_validate(p) for p in items],
        total=total,
        page=page,
        size=size,
        counts=counts,
    )


@router.post(
    "/me/publications", response_model=PublicationRead, status_code=status.HTTP_201_CREATED
)
async def add_publication(
    body: ManualPublicationCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PublicationRead:
    """手动补录（arxiv_id | doi | bibtex），直接进已确认列表。"""
    try:
        pub = await publications_service.add_manual_publication(
            session, user_id=user.id, arxiv_id=body.arxiv_id, doi=body.doi, bibtex=body.bibtex
        )
    except ParseFailedError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    return PublicationRead.model_validate(pub)


async def _set_status(
    session: AsyncSession, publication_id: uuid.UUID, user: User, new_status: str
) -> PublicationRead:
    pub = await publications_service.get_publication(
        session, user_id=user.id, publication_id=publication_id
    )
    if pub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PUBLICATION_NOT_FOUND")
    pub = await publications_service.set_status(session, publication=pub, status=new_status)
    return PublicationRead.model_validate(pub)


@router.post("/me/publications/{publication_id}/confirm", response_model=PublicationRead)
async def confirm_publication(
    publication_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PublicationRead:
    return await _set_status(session, publication_id, user, "confirmed")


@router.post("/me/publications/{publication_id}/reject", response_model=PublicationRead)
async def reject_publication(
    publication_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PublicationRead:
    """「不是我的」：保留 rejected 记录，阻止下次同步再推成候选。"""
    return await _set_status(session, publication_id, user, "rejected")
