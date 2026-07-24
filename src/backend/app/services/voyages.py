"""Voyage 业务逻辑（不 import fastapi）。"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.project import ProjectMember
from app.models.user import User
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


async def _is_project_member(
    session: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    row = await session.execute(
        select(ProjectMember.user_id).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    )
    return row.first() is not None


async def get_voyage(
    session: AsyncSession,
    *,
    voyage_id: uuid.UUID,
    user_id: uuid.UUID,
    with_steps: bool = False,
    user: User | None = None,
) -> VoyageRun | None:
    """取航程；无访问权视为不存在（返回 None）。

    访问权：起源课题成员（项目作用域任务）∪ 可管理其方向库者（P9a 库化任务——独立库
    无课题，鉴权走库级写权限：成员/策展人/admin）。库级鉴权需要 ``user``（角色/策展人
    判定），故 API 层传完整 user；仅传 user_id 时退化为项目成员判定。
    """
    stmt = select(VoyageRun).where(VoyageRun.id == voyage_id)
    if with_steps:
        stmt = stmt.options(selectinload(VoyageRun.steps))
    run = (await session.execute(stmt)).scalar_one_or_none()
    if run is None:
        return None
    if run.project_id is not None and await _is_project_member(
        session, project_id=run.project_id, user_id=user_id
    ):
        return run
    if run.library_id is not None and user is not None:
        from app.services.libraries import can_manage_library, get_library

        library = await get_library(session, run.library_id)
        if library is not None and await can_manage_library(
            session, user=user, library=library
        ):
            return run
    return None


async def cancel_voyage(session: AsyncSession, run: VoyageRun) -> VoyageRun:
    """协作式取消：置 cancelled，运行中的引擎在下一步边界自行退出。"""
    if run.status in TERMINAL_STATUSES:
        raise VoyageAlreadyFinishedError(str(run.id))
    run.status = "cancelled"
    await session.commit()
    await session.refresh(run)
    return run
