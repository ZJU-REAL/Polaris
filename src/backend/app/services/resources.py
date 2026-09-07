"""资源登记 CRUD + 通用连接凭据（R2 #677，不 import fastapi）。

凭据的通用创建也放这里（而不是塞进 ssh_credentials.py）：那个模块是 ssh 专用
老面（私钥专列 + asyncssh 连通性测试），继续原样服务老端点；多态凭据是资源
体系的配套件，跟资源 CRUD 一起演化。
"""

import json
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encrypt_secret
from app.models.resource import RESOURCE_KINDS, Resource
from app.models.ssh_credential import ConnectionCredential
from app.schemas.resource import ConnectionCredentialCreate, ResourceCreate, ResourceUpdate


class ResourceConfigError(ValueError):
    """kind 特定配置不合法（缺必填键等），API 层转 422。"""


# kind → config 必填键（非敏感；敏感字段一律进凭据 payload）
_REQUIRED_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "queue": ("queue",),  # 队列名
    "license_pool": ("feature",),  # License feature 名（如 "HFSS"）
    "host": (),
    "instrument": (),
}


def validate_kind_config(kind: str, config: dict | None) -> None:
    """轻校验：kind 合法 + 必填键在场且是非空字符串。不做白名单封闭
    （config 是各 runner 后端的自留地，键集合随后端演化）。"""
    if kind not in RESOURCE_KINDS:
        raise ResourceConfigError(f"unknown resource kind: {kind}")
    for key in _REQUIRED_CONFIG_KEYS[kind]:
        value = (config or {}).get(key)
        if not isinstance(value, str) or not value.strip():
            raise ResourceConfigError(f"resource kind '{kind}' requires config.{key}")


# ---- 资源 CRUD（owner 范围） ----


async def create_resource(
    session: AsyncSession, *, owner_id: uuid.UUID, data: ResourceCreate
) -> Resource:
    validate_kind_config(data.kind, data.config)
    resource = Resource(
        owner_id=owner_id,
        name=data.name,
        kind=data.kind,
        capacity=data.capacity,
        exclusive=data.exclusive,
        credential_id=data.credential_id,
        config=data.config,
    )
    session.add(resource)
    await session.commit()
    await session.refresh(resource)
    return resource


async def list_resources(session: AsyncSession, *, owner_id: uuid.UUID) -> Sequence[Resource]:
    stmt = (
        select(Resource)
        .where(Resource.owner_id == owner_id)
        .order_by(Resource.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def get_owned_resource(
    session: AsyncSession, *, resource_id: uuid.UUID, owner_id: uuid.UUID
) -> Resource | None:
    """取资源；非本人视为不存在（返回 None，同凭据口径不泄露存在性）。"""
    resource = await session.get(Resource, resource_id)
    if resource is None or resource.owner_id != owner_id:
        return None
    return resource


async def update_resource(
    session: AsyncSession, resource: Resource, data: ResourceUpdate
) -> Resource:
    fields = data.model_dump(exclude_unset=True)
    if "config" in fields:
        validate_kind_config(resource.kind, fields["config"])
    for key, value in fields.items():
        setattr(resource, key, value)
    await session.commit()
    await session.refresh(resource)
    return resource


async def delete_resource(session: AsyncSession, resource: Resource) -> None:
    # 租约行随 CASCADE 一起删；活租约的 run 由终态兜底不至于悬空占用
    await session.delete(resource)
    await session.commit()


async def get_owned_credential(
    session: AsyncSession, *, credential_id: uuid.UUID, owner_id: uuid.UUID
) -> ConnectionCredential | None:
    credential = await session.get(ConnectionCredential, credential_id)
    if credential is None or credential.user_id != owner_id:
        return None
    return credential


# ---- 通用凭据（多态） ----


class CredentialPayloadError(ValueError):
    """凭据载荷不合法（ssh 缺 private_key 等），API 层转 422。"""


async def create_connection_credential(
    session: AsyncSession, *, user_id: uuid.UUID, data: ConnectionCredentialCreate
) -> ConnectionCredential:
    """通用创建：ssh 落存量专列（消费方零改动），其余 kind 整包加密进 payload。"""
    private_key_encrypted = passphrase_encrypted = payload_encrypted = None
    if data.kind == "ssh":
        private_key = data.payload.get("private_key")
        if not isinstance(private_key, str) or not private_key:
            raise CredentialPayloadError("ssh credential requires payload.private_key")
        if not data.username:
            raise CredentialPayloadError("ssh credential requires username")
        private_key_encrypted = encrypt_secret(private_key)
        passphrase = data.payload.get("passphrase")
        if passphrase:
            passphrase_encrypted = encrypt_secret(str(passphrase))
    else:
        if data.kind == "visa" and not str(data.payload.get("resource", "")).strip():
            raise CredentialPayloadError("visa credential requires payload.resource")
        payload_encrypted = encrypt_secret(json.dumps(data.payload, ensure_ascii=False))
    credential = ConnectionCredential(
        user_id=user_id,
        kind=data.kind,
        name=data.name,
        host=data.host,
        port=data.port,
        username=data.username,
        private_key_encrypted=private_key_encrypted,
        passphrase_encrypted=passphrase_encrypted,
        payload_encrypted=payload_encrypted,
        proxy_url=data.proxy_url,
    )
    session.add(credential)
    await session.commit()
    await session.refresh(credential)
    return credential


async def list_connection_credentials(
    session: AsyncSession, *, user_id: uuid.UUID, kind: str | None = None
) -> Sequence[ConnectionCredential]:
    stmt = (
        select(ConnectionCredential)
        .where(ConnectionCredential.user_id == user_id)
        .order_by(ConnectionCredential.created_at.desc())
    )
    if kind:
        stmt = stmt.where(ConnectionCredential.kind == kind)
    return (await session.execute(stmt)).scalars().all()


async def delete_connection_credential(
    session: AsyncSession, credential: ConnectionCredential
) -> None:
    await session.delete(credential)
    await session.commit()
