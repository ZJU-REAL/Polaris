"""SSH 凭据路由（docs/api-m4.md §1）：每用户私有 CRUD + 连通性测试。

- 绝不回传私钥/口令（Read schema 不含相关字段）；
- 只能操作自己的凭据，他人凭据一律 404（不泄露存在性）。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.ssh_credential import SSHCredential
from app.models.user import User
from app.schemas.ssh_credential import SSHCredentialCreate, SSHCredentialRead, SSHTestResult
from app.services import ssh_credentials as credentials_service
from app.services import ssh_exec

router = APIRouter(prefix="/ssh-credentials", tags=["ssh-credentials"])


async def _get_owned(session: AsyncSession, credential_id: uuid.UUID, user: User) -> SSHCredential:
    credential = await credentials_service.get_owned_credential(
        session, credential_id=credential_id, user_id=user.id
    )
    if credential is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CREDENTIAL_NOT_FOUND")
    return credential


@router.get("", response_model=list[SSHCredentialRead])
async def list_credentials(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[SSHCredentialRead]:
    credentials = await credentials_service.list_credentials(session, user_id=user.id)
    return [SSHCredentialRead.model_validate(c) for c in credentials]


@router.post("", response_model=SSHCredentialRead, status_code=status.HTTP_201_CREATED)
async def create_credential(
    data: SSHCredentialCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SSHCredentialRead:
    credential = await credentials_service.create_credential(session, user_id=user.id, data=data)
    return SSHCredentialRead.model_validate(credential)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    credential_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    credential = await _get_owned(session, credential_id, user)
    await credentials_service.delete_credential(session, credential)


@router.post("/{credential_id}/test", response_model=SSHTestResult)
async def test_credential(
    credential_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SSHTestResult:
    """asyncssh 连接 + ``echo ok``；成功则更新 last_verified_at。"""
    credential = await _get_owned(session, credential_id, user)
    ok, detail = await ssh_exec.test_credential(credential)
    if ok:
        await credentials_service.mark_verified(session, credential)
    return SSHTestResult(ok=ok, detail=detail)


@router.get("/{credential_id}/sysinfo")
async def credential_sysinfo(
    credential_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict:
    """服务器系统状态一览（CPU/内存/磁盘/GPU；固定模板探测，连接失败 ok=false）。"""
    credential = await _get_owned(session, credential_id, user)
    return await ssh_exec.probe_sysinfo(credential)
