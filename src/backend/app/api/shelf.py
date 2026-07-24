"""课题「相关研究」书架路由（P5a）：/projects/{pid}/shelf。

成员鉴权与现有 projects 一致（非成员一律 404）。业务规则在
services/topic_shelf.py：入架落 wiki 快照 + 同步 upsert 个人库；
移出只删书架行；个人补充入库不建方向库成员行。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.schemas.shelf import (
    ShelfAddRequest,
    ShelfIdsRead,
    ShelfImportRequest,
    ShelfItemRead,
    ShelfNoteUpdate,
    ShelfPage,
)
from app.services import paper_import as paper_import_service
from app.services import projects as projects_service
from app.services import topic_shelf as shelf_service

router = APIRouter(tags=["shelf"])


async def _require_member(session: AsyncSession, project_id: uuid.UUID, user: User) -> None:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")


@router.get("/projects/{project_id}/shelf", response_model=ShelfPage)
async def list_shelf(
    project_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None),
    author: str | None = Query(default=None),
    affiliation: str | None = Query(default=None),
    year_from: int | None = Query(default=None),
    year_to: int | None = Query(default=None),
    reading_status: str | None = Query(default=None, pattern="^(unread|reading|read)$"),
    starred: bool | None = Query(default=None),
    sort: str = Query(default="added", pattern="^(added|year|relevance|title)$"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ShelfPage:
    await _require_member(session, project_id, user)
    items, total = await shelf_service.list_shelf(
        session,
        project_id=project_id,
        user_id=user.id,
        page=page,
        size=size,
        q=q,
        author=author,
        affiliation=affiliation,
        year_from=year_from,
        year_to=year_to,
        reading_status=reading_status,
        starred=starred,
        sort=sort,
    )
    return ShelfPage(
        items=[ShelfItemRead.model_validate(i) for i in items],
        total=total,
        page=page,
        size=size,
    )


@router.get("/projects/{project_id}/shelf/ids", response_model=ShelfIdsRead)
async def list_shelf_ids(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ShelfIdsRead:
    await _require_member(session, project_id, user)
    ids = await shelf_service.shelf_paper_ids(session, project_id=project_id)
    return ShelfIdsRead(paper_ids=ids)


@router.post(
    "/projects/{project_id}/shelf",
    response_model=ShelfItemRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_to_shelf(
    project_id: uuid.UUID,
    body: ShelfAddRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ShelfItemRead:
    """入架（重复入架幂等更新 note）；同步收藏进个人库。"""
    await _require_member(session, project_id, user)
    try:
        item = await shelf_service.add_to_shelf(
            session,
            project_id=project_id,
            paper_id=body.paper_id,
            user_id=user.id,
            note=body.note,
        )
    except shelf_service.PaperNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND") from None
    return ShelfItemRead.model_validate(item)


@router.post(
    "/projects/{project_id}/shelf/import",
    response_model=ShelfItemRead,
    status_code=status.HTTP_201_CREATED,
)
async def import_to_shelf(
    project_id: uuid.UUID,
    body: ShelfImportRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ShelfItemRead:
    """个人补充入库：查池命中直接入架；未命中抓取解析入池（不进任何方向库）再入架。"""
    await _require_member(session, project_id, user)
    try:
        item = await shelf_service.import_to_shelf(
            session,
            project_id=project_id,
            user_id=user.id,
            arxiv_id=body.arxiv_id,
            doi=body.doi,
            title=body.title,
        )
    except paper_import_service.ParseFailedError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"PARSE_FAILED: {e}"
        ) from e
    return ShelfItemRead.model_validate(item)


@router.patch("/projects/{project_id}/shelf/{paper_id}", response_model=ShelfItemRead)
async def update_shelf_note(
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    body: ShelfNoteUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ShelfItemRead:
    await _require_member(session, project_id, user)
    try:
        item = await shelf_service.update_note(
            session, project_id=project_id, paper_id=paper_id, user_id=user.id, note=body.note
        )
    except shelf_service.ShelfItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SHELF_ITEM_NOT_FOUND") from None
    return ShelfItemRead.model_validate(item)


@router.post(
    "/projects/{project_id}/shelf/{paper_id}/refresh-snapshot", response_model=ShelfItemRead
)
async def refresh_shelf_snapshot(
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ShelfItemRead:
    """手动刷新快照：从当前可得的最优 wiki（库版 > 个人版）重拷；无来源 409。"""
    await _require_member(session, project_id, user)
    try:
        item = await shelf_service.refresh_snapshot(
            session, project_id=project_id, paper_id=paper_id, user_id=user.id
        )
    except shelf_service.ShelfItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SHELF_ITEM_NOT_FOUND") from None
    except shelf_service.NoWikiSourceError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="NO_WIKI_SOURCE") from None
    return ShelfItemRead.model_validate(item)


@router.delete(
    "/projects/{project_id}/shelf/{paper_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_from_shelf(
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """移出书架：只删书架行，个人库条目不动。"""
    await _require_member(session, project_id, user)
    try:
        await shelf_service.remove_from_shelf(
            session, project_id=project_id, paper_id=paper_id
        )
    except shelf_service.ShelfItemNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SHELF_ITEM_NOT_FOUND") from None
