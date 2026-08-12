"""项目业务逻辑（不 import fastapi）。"""

import re
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectInvite, ProjectMember
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectUpdate

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# 稀疏 definition 缺 arxiv_categories 时的检索默认分类（actions_wiki 也用）
DEFAULT_ARXIV_CATEGORIES = ["cs.CL", "cs.AI", "cs.LG"]


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


async def _unique_slug(session: AsyncSession, base: str) -> str:
    slug = base
    while (await session.execute(select(Project.id).where(Project.slug == slug))).first():
        slug = f"{base}-{uuid.uuid4().hex[:6]}"
    return slug


def is_admin_expr(user_id: uuid.UUID):
    """SQL 层的「这个人是平台管理员吗」。

    这些查询只拿得到 user_id、拿不到 User 对象，所以用 exists 子查询就地判；
    写法与 voyages._visible_filter、libraries.user_visible_paper_stmt 一致。

    只读账号（游客）不算：它顶着 role=admin 是为了看见管理端长什么样，不是为了
    看见全实验室在做哪些课题。少了这一条，一个演示号打开就是二十几个课题的清单，
    每个名字都在说这个实验室正在做什么。它该看到的只有别人把它加进去的那些。
    """
    return (
        select(User.id)
        .where(User.id == user_id, User.role == "admin", User.read_only.is_(False))
        .exists()
    )


def in_my_projects(project_id_col, user_id: uuid.UUID):
    """「这条记录所属的课题我够得着吗」——课题作用域读取口的统一条件。

    够得着 = 我是该课题成员，或我是平台管理员（最高权限，全实验室可见可操作）。
    课题下的东西（想法/实验/稿件/闸门/书架论文……）一律用这一条判，口径必须一致：
    某处漏了 admin 分支，就会出现「列表里看得到、点进去 404」或「有任务却查不到
    课题名字」这类前后不一致。
    """
    return or_(
        project_id_col.in_(
            select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
        ),
        is_admin_expr(user_id),
    )


async def list_projects(session: AsyncSession, user_id: uuid.UUID) -> Sequence[Project]:
    """列出用户够得着的课题：本人参与的；平台管理员看全部。

    管理员必须看到全部——否则跟「任务列表对管理员是全平台可见」对不上：
    实验室工作台拿任务的 project_id 回查这份列表，查空就把归属显示成「未知课题」。
    """
    stmt = (
        select(Project)
        .where(in_my_projects(Project.id, user_id))
        .order_by(Project.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def list_projects_page(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    query: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[Sequence[Project], int]:
    """Paginate projects visible to a user for project discovery surfaces."""
    filters = [in_my_projects(Project.id, user_id)]
    if query:
        pattern = f"%{query}%"
        filters.append(or_(Project.name.ilike(pattern), Project.slug.ilike(pattern)))
    if status:
        filters.append(Project.status == status)

    total = await session.scalar(select(func.count()).select_from(Project).where(*filters))
    stmt = (
        select(Project)
        .where(*filters)
        .order_by(Project.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    projects = (await session.execute(stmt)).scalars().all()
    return projects, int(total or 0)


async def create_project(
    session: AsyncSession, owner_id: uuid.UUID, data: ProjectCreate
) -> Project:
    """建课题并把 owner 记为成员（role=owner）。

    P9c：课题不再拥有库，也不拥有收录配置——不自动建隐式库、不写收录配置。
    只建 project（name + 一句话 statement 存入 ``project.statement`` 供课题语境
    提示）+ 按 ``source_library_ids`` 关联**已有**文献库（可为空，空=课题暂无语料，
    各消费端给空态）。文献库全部是独立创建、管理员审批的（P9b）。
    """
    slug = await _unique_slug(session, data.slug or slugify(data.name))
    statement = (data.statement or "").strip()
    project = Project(
        name=data.name,
        slug=slug,
        statement=statement or None,
        owner_id=owner_id,
    )
    session.add(project)
    await session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=owner_id, role="owner"))
    if data.source_library_ids:
        from app.services.libraries import set_source_libraries

        await set_source_libraries(
            session, topic_id=project.id, library_ids=list(data.source_library_ids)
        )
    await session.commit()
    await session.refresh(project)
    return project


async def get_project(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> Project | None:
    """取项目；非成员视为不存在（返回 None）。平台管理员够得着全部课题。

    列表看得到的，点进去必须打得开——这是 :func:`list_projects` 的单条镜像，
    两边一起改。课题作用域的读取口（想法/实验/闸门等）大多经由这里鉴权。
    """
    stmt = select(Project).where(
        Project.id == project_id, in_my_projects(Project.id, user_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_members(session: AsyncSession, project_id: uuid.UUID) -> list[dict[str, object]]:
    """项目成员（附 email / display_name，供 detail 返回）。"""
    stmt = (
        select(ProjectMember, User.email, User.display_name)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.created_at)
    )
    return [
        {
            "project_id": member.project_id,
            "user_id": member.user_id,
            "role": member.role,
            "email": email,
            "display_name": display_name,
        }
        for member, email, display_name in (await session.execute(stmt)).all()
    ]


def can_manage_project(project: Project, user: User) -> bool:
    """PATCH / 加成员权限：项目 owner 或平台 admin。"""
    return project.owner_id == user.id or user.role == "admin"


async def update_project(session: AsyncSession, project: Project, data: ProjectUpdate) -> Project:
    if data.name is not None:
        project.name = data.name
    if data.statement is not None:
        project.statement = data.statement.strip() or None
    if data.status is not None:
        project.status = data.status
    await session.commit()
    await session.refresh(project)
    return project


async def delete_project(session: AsyncSession, project: Project) -> None:
    """删除项目；论文/概念/任务等子表靠 FK ondelete=CASCADE 一并清除。"""
    await session.delete(project)
    await session.commit()


async def add_member(
    session: AsyncSession, project_id: uuid.UUID, *, email: str, role: str
) -> bool:
    """按 email 把用户加入项目（已是成员则更新角色）。用户不存在返回 False。"""
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        return False
    return await add_member_by_id(session, project_id, user_id=user.id, role=role)


async def add_member_by_id(
    session: AsyncSession, project_id: uuid.UUID, *, user_id: uuid.UUID, role: str = "member"
) -> bool:
    """按 user_id 加入项目（已是成员则更新角色）。用户不存在返回 False。"""
    user = await session.get(User, user_id)
    if user is None:
        return False
    member = await session.get(ProjectMember, (project_id, user_id))
    if member is None:
        session.add(ProjectMember(project_id=project_id, user_id=user_id, role=role))
    else:
        member.role = role
    await session.commit()
    return True


async def remove_member(
    session: AsyncSession, project_id: uuid.UUID, *, user_id: uuid.UUID
) -> None:
    member = await session.get(ProjectMember, (project_id, user_id))
    if member is not None:
        await session.delete(member)
        await session.commit()


async def list_members_detailed(session: AsyncSession, project: Project) -> list[dict[str, Any]]:
    """项目成员明细（协作者面板用）：user_id / email / display_name / role / is_owner。"""
    stmt = (
        select(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project.id)
        .order_by(User.display_name, User.email)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "user_id": str(u.id),
            "email": u.email,
            "display_name": u.display_name,
            "role": m.role,
            "is_owner": project.owner_id == u.id,
        }
        for m, u in rows
    ]


# ---- 邀请链接 ----


class InviteInvalidError(Exception):
    """邀请链接不存在/已撤销/已过期/次数用尽。"""


async def create_invite(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    created_by: uuid.UUID,
    expires_days: int | None,
    max_uses: int | None,
) -> ProjectInvite:
    invite = ProjectInvite(
        project_id=project_id,
        token=secrets.token_urlsafe(24),
        created_by=created_by,
        expires_at=(datetime.now(UTC) + timedelta(days=expires_days)) if expires_days else None,
        max_uses=max_uses,
    )
    session.add(invite)
    await session.commit()
    await session.refresh(invite)
    return invite


async def list_invites(session: AsyncSession, project_id: uuid.UUID) -> Sequence[ProjectInvite]:
    stmt = (
        select(ProjectInvite)
        .where(ProjectInvite.project_id == project_id, ProjectInvite.revoked.is_(False))
        .order_by(ProjectInvite.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


def _invite_usable(invite: ProjectInvite) -> bool:
    if invite.revoked:
        return False
    if invite.expires_at is not None:
        expires = invite.expires_at
        if expires.tzinfo is None:  # sqlite 存储丢 tz
            expires = expires.replace(tzinfo=UTC)
        if expires < datetime.now(UTC):
            return False
    return not (invite.max_uses is not None and invite.used_count >= invite.max_uses)


async def resolve_invite(
    session: AsyncSession, token: str, *, user_id: uuid.UUID
) -> dict[str, Any]:
    """邀请预览：项目名 / 邀请人 / 是否有效 / 是否已是成员。"""
    invite = (
        await session.execute(select(ProjectInvite).where(ProjectInvite.token == token))
    ).scalar_one_or_none()
    if invite is None:
        raise InviteInvalidError
    project = await session.get(Project, invite.project_id)
    if project is None:
        raise InviteInvalidError
    inviter = await session.get(User, invite.created_by) if invite.created_by else None
    member = await session.get(ProjectMember, (invite.project_id, user_id))
    return {
        "project_id": project.id,
        "project_name": project.name,
        "inviter_name": (inviter.display_name or inviter.email) if inviter else None,
        "valid": _invite_usable(invite),
        "already_member": member is not None,
    }


async def accept_invite(session: AsyncSession, token: str, *, user_id: uuid.UUID) -> Project:
    """接受邀请：加入为 member（已是成员则幂等返回项目）。"""
    invite = (
        await session.execute(select(ProjectInvite).where(ProjectInvite.token == token))
    ).scalar_one_or_none()
    if invite is None:
        raise InviteInvalidError
    project = await session.get(Project, invite.project_id)
    if project is None:
        raise InviteInvalidError
    member = await session.get(ProjectMember, (invite.project_id, user_id))
    if member is not None:
        return project
    if not _invite_usable(invite):
        raise InviteInvalidError
    session.add(ProjectMember(project_id=invite.project_id, user_id=user_id, role="member"))
    invite.used_count += 1
    await session.commit()
    return project


async def revoke_invite(session: AsyncSession, invite_id: uuid.UUID, project_id: uuid.UUID) -> bool:
    invite = await session.get(ProjectInvite, invite_id)
    if invite is None or invite.project_id != project_id:
        return False
    invite.revoked = True
    await session.commit()
    return True
