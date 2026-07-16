"""研究方向邀请链接：成员生成/管理，持链接的登录用户自助加入。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.schemas.project import InviteCreate, InviteInfo, InviteRead, ProjectRead
from app.services import projects as projects_service

router = APIRouter(tags=["invites"])


async def _member_project(session: AsyncSession, project_id: uuid.UUID, user: User):
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    return project


@router.post(
    "/projects/{project_id}/invites",
    response_model=InviteRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite(
    project_id: uuid.UUID,
    data: InviteCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> InviteRead:
    await _member_project(session, project_id, user)
    invite = await projects_service.create_invite(
        session,
        project_id=project_id,
        created_by=user.id,
        expires_days=data.expires_days,
        max_uses=data.max_uses,
    )
    return InviteRead.model_validate(invite)


@router.get("/projects/{project_id}/invites", response_model=list[InviteRead])
async def list_invites(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[InviteRead]:
    await _member_project(session, project_id, user)
    invites = await projects_service.list_invites(session, project_id)
    return [InviteRead.model_validate(i) for i in invites]


@router.delete("/projects/{project_id}/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    project_id: uuid.UUID,
    invite_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    await _member_project(session, project_id, user)
    if not await projects_service.revoke_invite(session, invite_id, project_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="INVITE_NOT_FOUND")


@router.get("/invites/{token}", response_model=InviteInfo)
async def invite_info(
    token: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> InviteInfo:
    try:
        info = await projects_service.resolve_invite(session, token, user_id=user.id)
    except projects_service.InviteInvalidError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="INVITE_NOT_FOUND") from e
    return InviteInfo(**info)


@router.post("/invites/{token}/accept", response_model=ProjectRead)
async def accept_invite(
    token: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ProjectRead:
    try:
        project = await projects_service.accept_invite(session, token, user_id=user.id)
    except projects_service.InviteInvalidError as e:
        raise HTTPException(status.HTTP_410_GONE, detail="INVITE_INVALID") from e
    return ProjectRead.model_validate(project)
