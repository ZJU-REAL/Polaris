"""论文笔记路由（docs/api-lit.md §2）：论文级 CRUD + 课题笔记本聚合。

P5b 起笔记挂 paper × author（跨课题共享）：列表只返回请求者本人的笔记，
非作者访问单条一律 404（平台 admin 例外，可管理他人笔记）。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.paper import PaperNote
from app.models.user import User
from app.schemas.note import NotebookPage, NoteCreate, NoteRead, NoteUpdate, NoteWithPaper
from app.services import notes as notes_service
from app.services import papers as papers_service
from app.services import projects as projects_service

router = APIRouter(tags=["notes"])


def _note_read(note: PaperNote, author_name: str) -> NoteRead:
    return NoteRead(
        id=note.id,
        paper_id=note.paper_id,
        author_id=note.author_id,
        author_name=author_name,
        content=note.content,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


async def _get_modifiable_note(
    session: AsyncSession, note_id: uuid.UUID, user: User
) -> tuple[PaperNote, str]:
    """取笔记：非作者（且非平台 admin）视为不存在 → 404。"""
    row = await notes_service.get_own_note(session, note_id=note_id, user=user)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="NOTE_NOT_FOUND")
    return row


@router.get("/papers/{paper_id}/notes", response_model=list[NoteRead])
async def list_paper_notes(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[NoteRead]:
    paper = await papers_service.get_paper_for_user(session, paper_id=paper_id, user_id=user.id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    rows = await notes_service.list_paper_notes(session, paper_id=paper_id, author_id=user.id)
    return [_note_read(note, author_name) for note, author_name in rows]


@router.post(
    "/papers/{paper_id}/notes", response_model=NoteRead, status_code=status.HTTP_201_CREATED
)
async def create_paper_note(
    paper_id: uuid.UUID,
    data: NoteCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> NoteRead:
    paper = await papers_service.get_paper_for_user(session, paper_id=paper_id, user_id=user.id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    note = await notes_service.create_note(
        session, paper_id=paper.id, author=user, content=data.content
    )
    return _note_read(note, notes_service.author_name_of(user.display_name, user.email))


@router.patch("/notes/{note_id}", response_model=NoteRead)
async def update_note(
    note_id: uuid.UUID,
    data: NoteUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> NoteRead:
    note, author_name = await _get_modifiable_note(session, note_id, user)
    note = await notes_service.update_note(session, note, content=data.content)
    return _note_read(note, author_name)


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    note, _ = await _get_modifiable_note(session, note_id, user)
    await notes_service.delete_note(session, note)


@router.get("/projects/{project_id}/notes", response_model=NotebookPage)
async def project_notebook(
    project_id: uuid.UUID,
    q: str | None = Query(default=None),
    paper_id: uuid.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> NotebookPage:
    """课题笔记本：我的论文笔记在本课题范围内的聚合视图（搜索 + 分页 + 按论文过滤）。"""
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    rows, total = await notes_service.list_project_notes(
        session,
        project_id=project_id,
        author_id=user.id,
        q=q,
        paper_id=paper_id,
        page=page,
        size=size,
    )
    items = [
        NoteWithPaper(**_note_read(note, author_name).model_dump(), paper_title=paper_title)
        for note, author_name, paper_title in rows
    ]
    return NotebookPage(items=items, total=total, page=page, size=size)
