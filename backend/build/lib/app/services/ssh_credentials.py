"""SSH 凭据业务逻辑（不 import fastapi）。私钥/口令 Fernet 加密入库，绝不回传。"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encrypt_secret
from app.models.base import utcnow
from app.models.ssh_credential import SSHCredential
from app.schemas.ssh_credential import SSHCredentialCreate


async def create_credential(
    session: AsyncSession, *, user_id: uuid.UUID, data: SSHCredentialCreate
) -> SSHCredential:
    credential = SSHCredential(
        user_id=user_id,
        name=data.name,
        host=data.host,
        port=data.port,
        username=data.username,
        private_key_encrypted=encrypt_secret(data.private_key),
        passphrase_encrypted=encrypt_secret(data.passphrase) if data.passphrase else None,
    )
    session.add(credential)
    await session.commit()
    await session.refresh(credential)
    return credential


async def list_credentials(session: AsyncSession, *, user_id: uuid.UUID) -> Sequence[SSHCredential]:
    stmt = (
        select(SSHCredential)
        .where(SSHCredential.user_id == user_id)
        .order_by(SSHCredential.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def get_owned_credential(
    session: AsyncSession, *, credential_id: uuid.UUID, user_id: uuid.UUID
) -> SSHCredential | None:
    """取凭据；非本人视为不存在（返回 None）。"""
    credential = await session.get(SSHCredential, credential_id)
    if credential is None or credential.user_id != user_id:
        return None
    return credential


async def delete_credential(session: AsyncSession, credential: SSHCredential) -> None:
    await session.delete(credential)
    await session.commit()


async def mark_verified(session: AsyncSession, credential: SSHCredential) -> SSHCredential:
    credential.last_verified_at = utcnow()
    await session.commit()
    await session.refresh(credential)
    return credential
