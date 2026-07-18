"""PDF 划线标注业务逻辑（不 import fastapi）。

权限约定同论文笔记（services/notes.py）：
- 读 = 项目成员（非成员视为不存在）；
- 改 / 删 = 标注作者或平台 admin。
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Paper, PaperHighlight
from app.models.project import ProjectMember
from app.models.user import User
from app.schemas.highlight import HighlightCreate
from app.services.notes import author_name_of


async def create_highlight(
    session: AsyncSession, *, paper: Paper, author: User, data: HighlightCreate
) -> PaperHighlight:
    hl = PaperHighlight(
        paper_id=paper.id,
        project_id=paper.project_id,
        author_id=author.id,
        page=data.page,
        rects=[r.model_dump() for r in data.rects],
        selected_text=data.selected_text,
        color=data.color,
        note=data.note,
    )
    session.add(hl)
    await session.commit()
    await session.refresh(hl)
    return hl


async def list_paper_highlights(
    session: AsyncSession, *, paper_id: uuid.UUID
) -> Sequence[tuple[PaperHighlight, str]]:
    """某论文的划线（按页码、再按创建时间排序），附作者展示名。"""
    stmt = (
        select(PaperHighlight, User.display_name, User.email)
        .join(User, User.id == PaperHighlight.author_id)
        .where(PaperHighlight.paper_id == paper_id)
        .order_by(PaperHighlight.page.asc(), PaperHighlight.created_at.asc())
    )
    rows = (await session.execute(stmt)).all()
    return [(hl, author_name_of(display_name, email)) for hl, display_name, email in rows]


async def get_highlight_for_member(
    session: AsyncSession, *, highlight_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[PaperHighlight, str] | None:
    """取划线（附作者展示名）；非项目成员视为不存在。"""
    stmt = (
        select(PaperHighlight, User.display_name, User.email)
        .join(User, User.id == PaperHighlight.author_id)
        .join(ProjectMember, ProjectMember.project_id == PaperHighlight.project_id)
        .where(PaperHighlight.id == highlight_id, ProjectMember.user_id == user_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    hl, display_name, email = row
    return hl, author_name_of(display_name, email)


def can_modify_highlight(hl: PaperHighlight, user: User) -> bool:
    """改 / 删权限：标注作者或平台 admin。"""
    return hl.author_id == user.id or user.role == "admin"


async def update_highlight(
    session: AsyncSession, hl: PaperHighlight, *, updates: dict[str, Any]
) -> PaperHighlight:
    """按 exclude_unset 后的字段增量更新（仅 color / note）。"""
    for key in ("color", "note"):
        if key in updates:
            setattr(hl, key, updates[key])
    await session.commit()
    await session.refresh(hl)
    return hl


async def delete_highlight(session: AsyncSession, hl: PaperHighlight) -> None:
    await session.delete(hl)
    await session.commit()
