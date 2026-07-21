"""个人文献库路由（issue #108）：/me/library，用户级、方向无关。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.paper import Paper
from app.models.user import User
from app.schemas.library import (
    LibraryEntryRead,
    LibraryNoteUpdate,
    LibraryPage,
    LibrarySaveRequest,
    LibraryStateRead,
    LibraryVisitCreate,
)
from app.services import papers as papers_service
from app.services import user_library as library_service

router = APIRouter(tags=["library"])


async def _get_member_paper(session: AsyncSession, paper_id: uuid.UUID, user: User) -> Paper:
    """校验论文可见性（所在方向的成员），防止越权拷快照。"""
    paper = await papers_service.get_paper_for_user(session, paper_id=paper_id, user_id=user.id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    return paper


async def _get_own_entry(session: AsyncSession, entry_id: uuid.UUID, user: User):
    entry = await library_service.get_entry(session, user_id=user.id, entry_id=entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ENTRY_NOT_FOUND")
    return entry


@router.get("/me/library", response_model=LibraryPage)
async def list_library(
    tab: str = Query(default="history", pattern="^(saved|history)$"),
    q: str | None = Query(default=None),
    sort: str = Query(default="recent", pattern="^(recent|title|visits)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryPage:
    items, total = await library_service.list_entries(
        session, user_id=user.id, tab=tab, q=q, sort=sort, page=page, size=size
    )
    return LibraryPage(
        items=[LibraryEntryRead.model_validate(e) for e in items],
        total=total,
        page=page,
        size=size,
    )


# 注意：/visits 与 /state 必须注册在 /{entry_id} 之前，避免被路径参数抢占


@router.post(
    "/me/library/visits", response_model=LibraryEntryRead, status_code=status.HTTP_201_CREATED
)
async def record_visit(
    body: LibraryVisitCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryEntryRead:
    """阅读页打开时上报一次浏览（跨方向按 arxiv/doi/标题合并到同一条目）。"""
    paper = await _get_member_paper(session, body.paper_id, user)
    entry = await library_service.record_visit(session, user_id=user.id, paper=paper)
    return LibraryEntryRead.model_validate(entry)


@router.delete("/me/library/visits", status_code=status.HTTP_204_NO_CONTENT)
async def clear_history(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """清空浏览记录：未收藏条目删除，已收藏条目保留但清零访问统计。"""
    await library_service.clear_history(session, user_id=user.id)


@router.get("/me/library/state", response_model=LibraryStateRead)
async def library_state(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryStateRead:
    paper = await _get_member_paper(session, paper_id, user)
    entry = await library_service.entry_for_paper(session, user_id=user.id, paper=paper)
    return LibraryStateRead(entry_id=entry.id if entry else None, saved=bool(entry and entry.saved))


@router.post("/me/library", response_model=LibraryEntryRead, status_code=status.HTTP_201_CREATED)
async def save_entry(
    body: LibrarySaveRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryEntryRead:
    """收藏：paper_id（从论文页收藏）或 entry_id（收藏一条浏览记录）。"""
    if body.paper_id is not None:
        paper = await _get_member_paper(session, body.paper_id, user)
        entry = await library_service.save_paper(session, user_id=user.id, paper=paper)
    else:
        entry = await _get_own_entry(session, body.entry_id, user)  # type: ignore[arg-type]
        entry = await library_service.set_saved(session, entry=entry, saved=True)
    return LibraryEntryRead.model_validate(entry)


@router.patch("/me/library/{entry_id}", response_model=LibraryEntryRead)
async def update_entry(
    entry_id: uuid.UUID,
    body: LibraryNoteUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryEntryRead:
    entry = await _get_own_entry(session, entry_id, user)
    entry.note = body.note
    await session.commit()
    await session.refresh(entry)
    return LibraryEntryRead.model_validate(entry)


@router.delete("/me/library/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_entry(
    entry_id: uuid.UUID,
    mode: str = Query(default="unsave", pattern="^(unsave|purge)$"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """unsave=取消收藏（浏览记录保留）；purge=彻底删除该条目。"""
    entry = await _get_own_entry(session, entry_id, user)
    if mode == "purge":
        await library_service.purge_entry(session, entry=entry)
    else:
        await library_service.set_saved(session, entry=entry, saved=False)
