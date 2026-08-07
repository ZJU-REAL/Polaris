"""Voyage 业务逻辑（不 import fastapi）。"""

import uuid
from collections.abc import Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.library_direction import DirectionLibrary, DirectionLibraryCurator
from app.models.project import ProjectMember
from app.models.user import User
from app.models.voyage import LIBRARY_KINDS, TERMINAL_STATUSES, VoyageRun
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


def _visible_filter(stmt, user_id: uuid.UUID):
    """可见任务 = 我所在课题的任务 ∪ 我能管的库的任务 ∪ 我自己发起的任务（∪ admin 全部）。

    这是 :func:`can_view_voyage` 的 SQL 镜像——列表里能看到的，点进去必须打得开。
    库任务不能只靠 project_id 判：独立库的任务 project_id 为空，按课题成员 join
    会把它们整个漏掉——而独立库是常态（P9c 起建课题不再自动建库）。

    「课题关联了某个库」不给可见性：关联只是拿它的语料，管不了它的建库任务
    （库级写权限 = 创建者/策展人/admin，见 libraries.can_manage_library）。
    """
    my_projects = select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
    my_curated = select(DirectionLibraryCurator.library_id).where(
        DirectionLibraryCurator.user_id == user_id
    )
    my_libraries = select(DirectionLibrary.id).where(
        or_(DirectionLibrary.submitted_by == user_id, DirectionLibrary.id.in_(my_curated))
    )
    # 只读账号（游客）不吃这条旁路：课题可见范围已经按成员关系收紧
    # （见 projects.is_admin_expr），任务这边不跟着收，列表里就会冒出一堆
    # 它够不着的课题的任务，标题旁边写着「未知课题」。
    is_admin = (
        select(User.id)
        .where(User.id == user_id, User.role == "admin", User.read_only.is_(False))
        .exists()
    )
    # 「我发起的」只对有作用域的任务生效：平台级任务（两个 id 都为空）恒为 admin-only。
    mine_scoped = and_(
        VoyageRun.created_by == user_id,
        or_(VoyageRun.project_id.is_not(None), VoyageRun.library_id.is_not(None)),
    )
    return stmt.where(
        or_(
            VoyageRun.project_id.in_(my_projects),
            VoyageRun.library_id.in_(my_libraries),
            mine_scoped,
            is_admin,
        )
    )


async def list_voyages(
    session: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID | None = None
) -> Sequence[VoyageRun]:
    """列出用户够得着的任务；``project_id`` 只取该课题自己的任务。

    按课题过滤时排除库任务（建库 / 增量更新）：那是文献库自己的事，在实验室
    工作台看。判据是 kind 而不是 library_id —— 库化改造前建的存量任务只挂了
    课题，没有 library_id，按后者判会把它们当成课题任务漏出来。
    """
    stmt = _visible_filter(select(VoyageRun), user_id).order_by(VoyageRun.created_at.desc())
    if project_id is not None:
        stmt = stmt.where(
            VoyageRun.project_id == project_id, VoyageRun.kind.not_in(LIBRARY_KINDS)
        )
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


async def can_view_voyage(
    session: AsyncSession, *, run: VoyageRun, user: User
) -> bool:
    """能否查看某个任务（详情/日志/SSE/取消/重试的统一口径）。

    - 平台管理员：全部；
    - 平台级任务（两个作用域 id 都为空，如每日新论文抓取）：仅平台管理员，口径与
      订阅分类管理、手动刷新、向量开关一致；
    - 我发起的任务（``created_by``）：自己点的那次运行，哪怕后来不再是该库策展人也看得到；
    - 课题作用域任务：课题成员；
    - 库作用域任务：能管这个库的人（创建者/策展人/admin，见 libraries.can_manage_library）。

    :func:`_visible_filter` 是它的 SQL 镜像，两边必须一起改。
    """
    if user.role == "admin" and not user.read_only:
        return True
    if run.project_id is None and run.library_id is None:
        return False
    if run.created_by is not None and run.created_by == user.id:
        return True
    if run.project_id is not None and await _is_project_member(
        session, project_id=run.project_id, user_id=user.id
    ):
        return True
    if run.library_id is not None:
        from app.services.libraries import can_manage_library, get_library

        library = await get_library(session, run.library_id)
        if library is not None and await can_manage_library(
            session, user=user, library=library
        ):
            return True
    return False


async def get_voyage(
    session: AsyncSession,
    *,
    voyage_id: uuid.UUID,
    user_id: uuid.UUID,
    with_steps: bool = False,
    user: User | None = None,
) -> VoyageRun | None:
    """取航程；无访问权视为不存在（返回 None）。访问权见 :func:`can_view_voyage`。

    鉴权要完整 ``user``（角色/创建者/策展人判定），只传 ``user_id`` 的调用点回查一次。
    """
    stmt = select(VoyageRun).where(VoyageRun.id == voyage_id)
    if with_steps:
        stmt = stmt.options(selectinload(VoyageRun.steps))
    run = (await session.execute(stmt)).scalar_one_or_none()
    if run is None:
        return None
    if user is None:
        user = await session.get(User, user_id)
        if user is None:
            return None
    return run if await can_view_voyage(session, run=run, user=user) else None


class VoyageStillRunningError(Exception):
    """任务还没结束，不能删——先取消。"""


async def delete_voyage(session: AsyncSession, run: VoyageRun) -> None:
    """删除任务记录。只允许删**已结束**的。

    还在跑的不能删：worker 那边仍在按这个 id 执行，行没了会一路报错到不知所云的地方。
    要删先取消（cancel 是协作式的，引擎在下一步边界自行退出）。

    级联是有意设计的：步骤与日志随任务删掉，而 **token 用量与实验记录只解引用**
    （SET NULL）——删掉一个任务不该把它花过的钱从账上抹掉。
    """
    if run.status not in TERMINAL_STATUSES:
        raise VoyageStillRunningError(str(run.id))
    await session.delete(run)
    await session.commit()


async def cancel_voyage(session: AsyncSession, run: VoyageRun) -> VoyageRun:
    """协作式取消：置 cancelled，运行中的引擎在下一步边界自行退出。

    等回答的提问一并置 superseded——任务都取消了，问题不再需要答案，
    也不能留着一个能把 cancelled 任务重新拉起来的回答入口。
    """
    if run.status in TERMINAL_STATUSES:
        raise VoyageAlreadyFinishedError(str(run.id))
    from app.services import voyage_messages as messages_service

    await messages_service.supersede_open_asks(session, run.id)
    run.status = "cancelled"
    await session.commit()
    await session.refresh(run)
    return run
