"""PDF 划线标注路由：论文级 CRUD（阅读器用）。

权限同论文笔记：项目成员可读，作者 / 平台 admin 可改删。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.paper import PaperHighlight
from app.models.user import User
from app.schemas.highlight import HighlightCreate, HighlightRead, HighlightUpdate
from app.services import highlights as hl_service
from app.services import papers as papers_service
from app.services.notes import author_name_of

router = APIRouter(tags=["highlights"])


def _hl_read(hl: PaperHighlight, author_name: str) -> HighlightRead:
    return HighlightRead(
        id=hl.id,
        paper_id=hl.paper_id,
        project_id=hl.project_id,
        author_id=hl.author_id,
        author_name=author_name,
        page=hl.page,
        rects=hl.rects,
        selected_text=hl.selected_text,
        color=hl.color,
        style=hl.style,
        note=hl.note,
        created_at=hl.created_at,
        updated_at=hl.updated_at,
    )


async def _get_modifiable_highlight(
    session: AsyncSession, highlight_id: uuid.UUID, user: User
) -> tuple[PaperHighlight, str]:
    """取划线：非项目成员 404；非作者且非平台 admin 403。"""
    row = await hl_service.get_highlight_for_member(
        session, highlight_id=highlight_id, user_id=user.id
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="HIGHLIGHT_NOT_FOUND")
    hl, author_name = row
    if not hl_service.can_modify_highlight(hl, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="HIGHLIGHT_FORBIDDEN")
    return hl, author_name


@router.get("/papers/{paper_id}/highlights", response_model=list[HighlightRead])
async def list_paper_highlights(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[HighlightRead]:
    paper = await papers_service.get_paper_for_user(session, paper_id=paper_id, user_id=user.id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    rows = await hl_service.list_paper_highlights(session, paper_id=paper_id)
    return [_hl_read(hl, author_name) for hl, author_name in rows]


@router.post(
    "/papers/{paper_id}/highlights",
    response_model=HighlightRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_paper_highlight(
    paper_id: uuid.UUID,
    data: HighlightCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> HighlightRead:
    paper = await papers_service.get_paper_for_user(session, paper_id=paper_id, user_id=user.id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    hl = await hl_service.create_highlight(
        session, paper_id=paper.id, project_id=paper.project_id, author=user, data=data
    )
    return _hl_read(hl, author_name_of(user.display_name, user.email))


@router.patch("/highlights/{highlight_id}", response_model=HighlightRead)
async def update_highlight(
    highlight_id: uuid.UUID,
    data: HighlightUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> HighlightRead:
    hl, author_name = await _get_modifiable_highlight(session, highlight_id, user)
    hl = await hl_service.update_highlight(session, hl, updates=data.model_dump(exclude_unset=True))
    return _hl_read(hl, author_name)


@router.delete("/highlights/{highlight_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_highlight(
    highlight_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    hl, _ = await _get_modifiable_highlight(session, highlight_id, user)
    await hl_service.delete_highlight(session, hl)
