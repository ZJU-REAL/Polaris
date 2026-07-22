"""论文笔记路由（docs/api-lit.md §2）：论文级 CRUD + 项目笔记本聚合。"""

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
        project_id=note.project_id,
        author_id=note.author_id,
        author_name=author_name,
        content=note.content,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


async def _get_modifiable_note(
    session: AsyncSession, note_id: uuid.UUID, user: User
) -> tuple[PaperNote, str]:
    """取笔记：非项目成员 404；非作者且非平台 admin 403。"""
    row = await notes_service.get_note_for_member(session, note_id=note_id, user_id=user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="NOTE_NOT_FOUND")
    note, author_name = row
    if not notes_service.can_modify_note(note, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="NOTE_FORBIDDEN")
    return note, author_name


@router.get("/papers/{paper_id}/notes", response_model=list[NoteRead])
async def list_paper_notes(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[NoteRead]:
    paper = await papers_service.get_paper_for_user(session, paper_id=paper_id, user_id=user.id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    rows = await notes_service.list_paper_notes(session, paper_id=paper_id)
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
        session,
        paper_id=paper.id,
        project_id=paper.project_id,
        author=user,
        content=data.content,
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
    """项目笔记本：全部论文笔记的聚合视图（搜索 + 分页 + 按论文过滤）。"""
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    rows, total = await notes_service.list_project_notes(
        session, project_id=project_id, q=q, paper_id=paper_id, page=page, size=size
    )
    items = [
        NoteWithPaper(**_note_read(note, author_name).model_dump(), paper_title=paper_title)
        for note, author_name, paper_title in rows
    ]
    return NotebookPage(items=items, total=total, page=page, size=size)
