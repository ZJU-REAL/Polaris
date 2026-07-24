"""项目业务逻辑（不 import fastapi）。"""

import re
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
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


async def list_projects(session: AsyncSession, user_id: uuid.UUID) -> Sequence[Project]:
    """列出用户参与（作为成员）的全部项目。"""
    stmt = (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id)
        .order_by(Project.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


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
    """取项目；非成员视为不存在（返回 None）。"""
    stmt = (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(Project.id == project_id, ProjectMember.user_id == user_id)
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
