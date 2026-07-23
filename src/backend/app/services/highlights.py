"""PDF 划线标注业务逻辑（不 import fastapi）。

归属与权限同论文笔记（services/notes.py，P5b 拆分）：
- 划线挂 paper × author，跨课题共享；
- 读 / 改 / 删都只限作者本人（平台 admin 可改删他人标注，管理兜底）。
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import PaperHighlight
from app.models.user import User
from app.schemas.highlight import HighlightCreate
from app.services.notes import author_name_of


async def create_highlight(
    session: AsyncSession,
    *,
    paper_id: uuid.UUID,
    author: User,
    data: HighlightCreate,
) -> PaperHighlight:
    hl = PaperHighlight(
        paper_id=paper_id,
        author_id=author.id,
        page=data.page,
        rects=[r.model_dump() for r in data.rects],
        selected_text=data.selected_text,
        color=data.color,
        style=data.style,
        note=data.note,
    )
    session.add(hl)
    await session.commit()
    await session.refresh(hl)
    return hl


async def list_paper_highlights(
    session: AsyncSession, *, paper_id: uuid.UUID, author_id: uuid.UUID
) -> Sequence[tuple[PaperHighlight, str]]:
    """某论文下「我的」划线（按页码、再按创建时间排序），附作者展示名。"""
    stmt = (
        select(PaperHighlight, User.display_name, User.email)
        .join(User, User.id == PaperHighlight.author_id)
        .where(PaperHighlight.paper_id == paper_id, PaperHighlight.author_id == author_id)
        .order_by(PaperHighlight.page.asc(), PaperHighlight.created_at.asc())
    )
    rows = (await session.execute(stmt)).all()
    return [(hl, author_name_of(display_name, email)) for hl, display_name, email in rows]


async def get_own_highlight(
    session: AsyncSession, *, highlight_id: uuid.UUID, user: User
) -> tuple[PaperHighlight, str] | None:
    """取划线（附作者展示名）；非作者（且非平台 admin）视为不存在。"""
    stmt = (
        select(PaperHighlight, User.display_name, User.email)
        .join(User, User.id == PaperHighlight.author_id)
        .where(PaperHighlight.id == highlight_id)
    )
    if user.role != "admin":
        stmt = stmt.where(PaperHighlight.author_id == user.id)
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    hl, display_name, email = row
    return hl, author_name_of(display_name, email)


async def update_highlight(
    session: AsyncSession, hl: PaperHighlight, *, updates: dict[str, Any]
) -> PaperHighlight:
    """按 exclude_unset 后的字段增量更新（仅 color / style / note）。"""
    for key in ("color", "style", "note"):
        if key in updates:
            setattr(hl, key, updates[key])
    await session.commit()
    await session.refresh(hl)
    return hl


async def delete_highlight(session: AsyncSession, hl: PaperHighlight) -> None:
    await session.delete(hl)
    await session.commit()
