"""项目路由（薄层，逻辑在 services/projects.py）。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.project import Project
from app.models.user import User
from app.schemas.libraries import DirectionLibrarySummary, SourceLibrariesUpdate
from app.schemas.project import (
    ProjectCreate,
    ProjectDetailRead,
    ProjectMemberAdd,
    ProjectMemberRead,
    ProjectRead,
    ProjectUpdate,
)
from app.services import libraries as libraries_service
from app.services import projects as projects_service

router = APIRouter(prefix="/projects", tags=["projects"])


async def _get_member_project(session: AsyncSession, project_id: uuid.UUID, user: User) -> Project:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    return project


async def _get_managed_project(session: AsyncSession, project_id: uuid.UUID, user: User) -> Project:
    project = await _get_member_project(session, project_id, user)
    if not projects_service.can_manage_project(project, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="PROJECT_FORBIDDEN")
    return project


async def _detail(session: AsyncSession, project: Project) -> ProjectDetailRead:
    members = await projects_service.list_members(session, project.id)
    # 不直接 model_validate(project)：ProjectDetailRead.members 会触发 ORM 关系懒加载
    base = ProjectRead.model_validate(project)
    return ProjectDetailRead(**base.model_dump(), members=[ProjectMemberRead(**m) for m in members])


@router.get("", response_model=list[ProjectRead])
async def list_projects(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ProjectRead]:
    projects = await projects_service.list_projects(session, user_id=user.id)
    return [ProjectRead.model_validate(p) for p in projects]


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    data: ProjectCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ProjectRead:
    project = await projects_service.create_project(session, owner_id=user.id, data=data)
    return ProjectRead.model_validate(project)


@router.get("/{project_id}", response_model=ProjectDetailRead)
async def get_project(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ProjectDetailRead:
    project = await _get_member_project(session, project_id, user)
    return await _detail(session, project)


@router.patch("/{project_id}", response_model=ProjectDetailRead)
async def update_project(
    project_id: uuid.UUID,
    data: ProjectUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ProjectDetailRead:
    project = await _get_managed_project(session, project_id, user)
    project = await projects_service.update_project(session, project, data)
    return await _detail(session, project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """删除研究方向（owner / 平台 admin），方向下的论文、概念、任务等一并删除。"""
    project = await _get_managed_project(session, project_id, user)
    await projects_service.delete_project(session, project)


@router.post("/{project_id}/members", status_code=status.HTTP_204_NO_CONTENT)
async def add_member(
    project_id: uuid.UUID,
    data: ProjectMemberAdd,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    project = await _get_managed_project(session, project_id, user)
    found = await projects_service.add_member(session, project.id, email=data.email, role=data.role)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="USER_NOT_FOUND")


@router.get("/{project_id}/source-libraries", response_model=list[DirectionLibrarySummary])
async def list_source_libraries(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[DirectionLibrarySummary]:
    """课题关联的文献库（课题语料 = 这些库论文的并集，按关联建立时间）。"""
    await _get_member_project(session, project_id, user)
    rows = await libraries_service.source_libraries_overview(
        session, topic_id=project_id, user=user
    )
    return [DirectionLibrarySummary(**row) for row in rows]


@router.put("/{project_id}/source-libraries", response_model=list[DirectionLibrarySummary])
async def set_source_libraries(
    project_id: uuid.UUID,
    data: SourceLibrariesUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[DirectionLibrarySummary]:
    """全量替换课题关联库（课题成员）。空列表 = 清空关联；不存在的库 id 静默忽略。

    关联为空时想法生成/检索/图谱/伴读等消费端给「还没关联文献库」空态，不报错。
    """
    await _get_member_project(session, project_id, user)
    await libraries_service.set_source_libraries(
        session, topic_id=project_id, library_ids=data.library_ids
    )
    await session.commit()
    rows = await libraries_service.source_libraries_overview(
        session, topic_id=project_id, user=user
    )
    return [DirectionLibrarySummary(**row) for row in rows]
