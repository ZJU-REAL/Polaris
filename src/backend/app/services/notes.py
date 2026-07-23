"""论文笔记业务逻辑（不 import fastapi）。

归属与权限（P5b 拆分，docs-dev/workspace-ia-redesign.md §3.3）：
- 笔记挂 paper × author，跨课题共享（同一篇论文的笔记在所有课题可见）；
- 读 / 改 / 删都只限作者本人（平台 admin 可改删他人笔记，管理兜底）。
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import LibraryPaper
from app.models.paper import Paper, PaperNote
from app.models.topic_shelf import TopicPaper
from app.models.user import User
from app.services.libraries import get_source_library_ids


def author_name_of(display_name: str | None, email: str) -> str:
    """展示名：display_name 回退 email @ 前部分。"""
    return (display_name or "").strip() or email.split("@", 1)[0]


async def create_note(
    session: AsyncSession, *, paper_id: uuid.UUID, author: User, content: str
) -> PaperNote:
    note = PaperNote(paper_id=paper_id, author_id=author.id, content=content)
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return note


async def list_paper_notes(
    session: AsyncSession, *, paper_id: uuid.UUID, author_id: uuid.UUID
) -> Sequence[tuple[PaperNote, str]]:
    """某论文下「我的」笔记（created_at 倒序），附作者展示名。"""
    stmt = (
        select(PaperNote, User.display_name, User.email)
        .join(User, User.id == PaperNote.author_id)
        .where(PaperNote.paper_id == paper_id, PaperNote.author_id == author_id)
        .order_by(PaperNote.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [(note, author_name_of(display_name, email)) for note, display_name, email in rows]


async def get_own_note(
    session: AsyncSession, *, note_id: uuid.UUID, user: User
) -> tuple[PaperNote, str] | None:
    """取笔记（附作者展示名）；非作者（且非平台 admin）视为不存在。"""
    stmt = (
        select(PaperNote, User.display_name, User.email)
        .join(User, User.id == PaperNote.author_id)
        .where(PaperNote.id == note_id)
    )
    if user.role != "admin":
        stmt = stmt.where(PaperNote.author_id == user.id)
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    note, display_name, email = row
    return note, author_name_of(display_name, email)


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
    author_id: uuid.UUID,
    q: str | None = None,
    paper_id: uuid.UUID | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[Sequence[tuple[PaperNote, str, str]], int]:
    """课题笔记本：「我的」笔记里落在本课题范围（方向库 ∪ 相关研究书架）的部分。

    分页 + 内容搜索 + 按论文过滤；返回 (rows, total)，row = (note, author_name, paper_title)。
    """
    # 课题范围 = 关联库并集 ∪ 相关研究书架；无关联库时只剩书架部分
    library_ids = await get_source_library_ids(session, project_id)
    scope_conditions = [
        PaperNote.paper_id.in_(
            select(TopicPaper.paper_id).where(TopicPaper.topic_id == project_id)
        )
    ]
    if library_ids:
        scope_conditions.append(
            PaperNote.paper_id.in_(
                select(LibraryPaper.paper_id).where(
                    LibraryPaper.library_id.in_(library_ids)
                )
            )
        )
    in_scope = or_(*scope_conditions)
    stmt = (
        select(PaperNote, User.display_name, User.email, Paper.title)
        .join(User, User.id == PaperNote.author_id)
        .join(Paper, Paper.id == PaperNote.paper_id)
        .where(PaperNote.author_id == author_id, in_scope)
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
