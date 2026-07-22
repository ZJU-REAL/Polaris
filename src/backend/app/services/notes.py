"""论文笔记业务逻辑（不 import fastapi）。

权限约定（docs/api-lit.md §2）：
- 读笔记 = 项目成员（非成员视为不存在）；
- 改 / 删 = 笔记作者或平台 admin。
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Paper, PaperNote
from app.models.project import ProjectMember
from app.models.user import User


def author_name_of(display_name: str | None, email: str) -> str:
    """展示名：display_name 回退 email @ 前部分。"""
    return (display_name or "").strip() or email.split("@", 1)[0]


async def create_note(
    session: AsyncSession, *, paper_id: uuid.UUID, project_id: uuid.UUID, author: User, content: str
) -> PaperNote:
    note = PaperNote(
        paper_id=paper_id, project_id=project_id, author_id=author.id, content=content
    )
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return note


async def list_paper_notes(
    session: AsyncSession, *, paper_id: uuid.UUID
) -> Sequence[tuple[PaperNote, str]]:
    """某论文的笔记（created_at 倒序），附作者展示名。"""
    stmt = (
        select(PaperNote, User.display_name, User.email)
        .join(User, User.id == PaperNote.author_id)
        .where(PaperNote.paper_id == paper_id)
        .order_by(PaperNote.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [(note, author_name_of(display_name, email)) for note, display_name, email in rows]


async def get_note_for_member(
    session: AsyncSession, *, note_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[PaperNote, str] | None:
    """取笔记（附作者展示名）；非项目成员视为不存在。"""
    stmt = (
        select(PaperNote, User.display_name, User.email)
        .join(User, User.id == PaperNote.author_id)
        .join(ProjectMember, ProjectMember.project_id == PaperNote.project_id)
        .where(PaperNote.id == note_id, ProjectMember.user_id == user_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    note, display_name, email = row
    return note, author_name_of(display_name, email)


def can_modify_note(note: PaperNote, user: User) -> bool:
    """改 / 删权限：笔记作者或平台 admin。"""
    return note.author_id == user.id or user.role == "admin"


async def update_note(session: AsyncSession, note: PaperNote, *, content: str) -> PaperNote:
    note.content = content
    await session.commit()
    await session.refresh(note)
    return note


async def delete_note(session: AsyncSession, note: PaperNote) -> None:
    await session.delete(note)
    await session.commit()


async def list_project_notes(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    q: str | None = None,
    paper_id: uuid.UUID | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[Sequence[tuple[PaperNote, str, str]], int]:
    """项目笔记本：分页 + 内容搜索 + 按论文过滤；返回 (rows, total)，
    row = (note, author_name, paper_title)。"""
    stmt = (
        select(PaperNote, User.display_name, User.email, Paper.title)
        .join(User, User.id == PaperNote.author_id)
        .join(Paper, Paper.id == PaperNote.paper_id)
        .where(PaperNote.project_id == project_id)
    )
    if q:
        stmt = stmt.where(PaperNote.content.ilike(f"%{q}%"))
    if paper_id is not None:
        stmt = stmt.where(PaperNote.paper_id == paper_id)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(PaperNote.created_at.desc()).offset((page - 1) * size).limit(size)
    rows = (await session.execute(stmt)).all()
    return [
        (note, author_name_of(display_name, email), title)
        for note, display_name, email, title in rows
    ], int(total)
