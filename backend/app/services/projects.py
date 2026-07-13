"""项目业务逻辑（不 import fastapi）。"""

import re
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectMember
from app.schemas.project import ProjectCreate

_SLUG_RE = re.compile(r"[^a-z0-9]+")


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
    """建项目并把 owner 记为成员（role=owner）。"""
    slug = await _unique_slug(session, data.slug or slugify(data.name))
    project = Project(
        name=data.name,
        slug=slug,
        definition=data.definition,
        owner_id=owner_id,
    )
    session.add(project)
    await session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=owner_id, role="owner"))
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
