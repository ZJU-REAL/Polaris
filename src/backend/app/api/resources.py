"""资源登记与通用连接凭据路由（R2 #677）。

- /resources：主机/队列/License 池/仪器的 owner 范围 CRUD；
- /connection-credentials：多态凭据（ssh|grpc|visa|http）。ssh 老端点
  /ssh-credentials 原样保留（前端 SSH 凭据 UI 不动）；本组端点暂无前端 UI，
  资源设置页归后续 PR（挂设置页「实验资源」分区）。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.resource import Resource
from app.models.user import User
from app.schemas.resource import (
    ConnectionCredentialCreate,
    ConnectionCredentialRead,
    ResourceCreate,
    ResourceRead,
    ResourceUpdate,
    RunnerHostRegister,
)
from app.services import byo_runner as byo_runner_service
from app.services import resources as resources_service

router = APIRouter(prefix="/resources", tags=["resources"])
credentials_router = APIRouter(prefix="/connection-credentials", tags=["connection-credentials"])


async def _get_owned(session: AsyncSession, resource_id: uuid.UUID, user: User) -> Resource:
    resource = await resources_service.get_owned_resource(
        session, resource_id=resource_id, owner_id=user.id
    )
    if resource is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="RESOURCE_NOT_FOUND")
    return resource


async def _check_credential_ref(
    session: AsyncSession, credential_id: uuid.UUID | None, user: User
) -> None:
    """credential_id 只能指向自己的凭据（他人凭据同样 404 不泄露存在性）。"""
    if credential_id is None:
        return
    credential = await resources_service.get_owned_credential(
        session, credential_id=credential_id, owner_id=user.id
    )
    if credential is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CREDENTIAL_NOT_FOUND")


@router.get("", response_model=list[ResourceRead])
async def list_resources(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ResourceRead]:
    resources = await resources_service.list_resources(session, owner_id=user.id)
    return [ResourceRead.model_validate(r) for r in resources]


@router.post("", response_model=ResourceRead, status_code=status.HTTP_201_CREATED)
async def create_resource(
    data: ResourceCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ResourceRead:
    await _check_credential_ref(session, data.credential_id, user)
    try:
        resource = await resources_service.create_resource(session, owner_id=user.id, data=data)
    except resources_service.ResourceConfigError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return ResourceRead.model_validate(resource)


@router.post("/runner-hosts", response_model=ResourceRead, status_code=status.HTTP_201_CREATED)
async def register_runner_host(
    data: RunnerHostRegister,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ResourceRead:
    """注册 BYO runner 主机（#685）：host 类 Resource + SSH 凭据关联，
    config.ephemeral 默认 true（推荐容器化执行不残留）。"""
    merged = dict(data.config or {})
    merged["ephemeral"] = data.ephemeral
    try:
        resource = await byo_runner_service.register_runner_host(
            session,
            owner_id=user.id,
            name=data.name,
            credential_id=data.credential_id,
            config=merged,
        )
    except byo_runner_service.RunnerHostCredentialError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CREDENTIAL_NOT_FOUND") from e
    except byo_runner_service.RunnerHostKindError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e
    return ResourceRead.model_validate(resource)


@router.get("/{resource_id}", response_model=ResourceRead)
async def get_resource(
    resource_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ResourceRead:
    return ResourceRead.model_validate(await _get_owned(session, resource_id, user))


@router.patch("/{resource_id}", response_model=ResourceRead)
async def update_resource(
    resource_id: uuid.UUID,
    data: ResourceUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ResourceRead:
    resource = await _get_owned(session, resource_id, user)
    if "credential_id" in data.model_dump(exclude_unset=True):
        await _check_credential_ref(session, data.credential_id, user)
    try:
        resource = await resources_service.update_resource(session, resource, data)
    except resources_service.ResourceConfigError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return ResourceRead.model_validate(resource)


@router.delete("/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_resource(
    resource_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    resource = await _get_owned(session, resource_id, user)
    await resources_service.delete_resource(session, resource)


# ---- 通用凭据端点（多态；响应绝不含密钥/载荷） ----


@credentials_router.get("", response_model=list[ConnectionCredentialRead])
async def list_credentials(
    kind: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ConnectionCredentialRead]:
    credentials = await resources_service.list_connection_credentials(
        session, user_id=user.id, kind=kind
    )
    return [ConnectionCredentialRead.model_validate(c) for c in credentials]


@credentials_router.post(
    "", response_model=ConnectionCredentialRead, status_code=status.HTTP_201_CREATED
)
async def create_credential(
    data: ConnectionCredentialCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ConnectionCredentialRead:
    try:
        credential = await resources_service.create_connection_credential(
            session, user_id=user.id, data=data
        )
    except resources_service.CredentialPayloadError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return ConnectionCredentialRead.model_validate(credential)


@credentials_router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    credential_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    credential = await resources_service.get_owned_credential(
        session, credential_id=credential_id, owner_id=user.id
    )
    if credential is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CREDENTIAL_NOT_FOUND")
    # 删除 = 吊销（#685）：有未终态实验引用 → 409；host 资源联动标记不可用
    try:
        await byo_runner_service.revoke_connection_credential(session, credential)
    except byo_runner_service.CredentialInUseError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="CREDENTIAL_IN_USE") from e
