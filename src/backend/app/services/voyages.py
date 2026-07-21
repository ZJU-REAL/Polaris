"""Voyage 业务逻辑（不 import fastapi）。"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.project import ProjectMember
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.voyage import VoyageCreate


class VoyageAlreadyFinishedError(Exception):
    """对终态航程执行 cancel。"""


async def create_voyage(
    session: AsyncSession, *, created_by: uuid.UUID, data: VoyageCreate
) -> VoyageRun:
    params = data.params or {}
    budget = params.get("budget") if isinstance(params.get("budget"), dict) else None
    run = VoyageRun(
        kind=data.kind,
        goal=data.goal,
        status="planning",
        cursor=0,
        checkpoint={"params": params} if params else None,
        budget=budget,
        project_id=data.project_id,
        created_by=created_by,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


def _member_filter(stmt, user_id: uuid.UUID):
    return stmt.join(ProjectMember, ProjectMember.project_id == VoyageRun.project_id).where(
        ProjectMember.user_id == user_id
    )


async def list_voyages(
    session: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID | None = None
) -> Sequence[VoyageRun]:
    """列出用户所在项目的航程（可按项目过滤）。"""
    stmt = _member_filter(select(VoyageRun), user_id).order_by(VoyageRun.created_at.desc())
    if project_id is not None:
        stmt = stmt.where(VoyageRun.project_id == project_id)
    return (await session.execute(stmt)).scalars().all()


async def get_voyage(
    session: AsyncSession,
    *,
    voyage_id: uuid.UUID,
    user_id: uuid.UUID,
    with_steps: bool = False,
) -> VoyageRun | None:
    """取航程；非项目成员视为不存在（返回 None）。"""
    stmt = _member_filter(select(VoyageRun), user_id).where(VoyageRun.id == voyage_id)
    if with_steps:
        stmt = stmt.options(selectinload(VoyageRun.steps))
    return (await session.execute(stmt)).scalar_one_or_none()


async def cancel_voyage(session: AsyncSession, run: VoyageRun) -> VoyageRun:
    """协作式取消：置 cancelled，运行中的引擎在下一步边界自行退出。"""
    if run.status in TERMINAL_STATUSES:
        raise VoyageAlreadyFinishedError(str(run.id))
    run.status = "cancelled"
    await session.commit()
    await session.refresh(run)
    return run
