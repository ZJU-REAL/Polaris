"""外部 MCP 服务器的管理面（#754）。

门槛是 **owner**，与插件市场同一条理由：登记一台 stdio 服务器等于声明「服务端进程
可以拉起这条命令」。那是在服务器上执行任意程序的能力，对多账号部署而言这个风险
属于所有人，不是登记者一个人的事。桌面档位下 owner 就是本人，行为无差别。

env 只进不出：写入时加密落库，读取时**永不回传**——回传等于给任何能打开管理面的
人一份明文密钥副本，而管理面的意义只是「改配置」，不是「看密钥」。
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models.mcp_server import MCP_TRANSPORTS, McpServer
from app.models.user import User
from app.services.mcp_hub import registry as mcp_registry
from app.services.owner import require_owner

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])

SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class McpServerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    transport: str
    command: str | None
    args: list[Any] | None
    cwd: str | None
    url: str | None
    enabled: bool
    last_tools: list[Any] | None
    last_error: str | None
    #: env 的**键名**（不含值）：既能看出配了哪些变量，又不泄露任何密钥
    env_keys: list[str] = []


class McpServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    title: str = Field(default="", max_length=128)
    transport: str = "stdio"
    command: str | None = Field(default=None, max_length=512)
    args: list[str] | None = None
    cwd: str | None = Field(default=None, max_length=1024)
    url: str | None = Field(default=None, max_length=1024)
    env: dict[str, str] | None = None
    enabled: bool = False


class McpServerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=128)
    command: str | None = Field(default=None, max_length=512)
    args: list[str] | None = None
    cwd: str | None = Field(default=None, max_length=1024)
    url: str | None = Field(default=None, max_length=1024)
    #: 不传 = 不动已存的 env；传 {} = 清空。读不回明文，所以必须能整体替换。
    env: dict[str, str] | None = None
    enabled: bool | None = None


def _read(row: McpServer) -> McpServerRead:
    out = McpServerRead.model_validate(row)
    out.env_keys = sorted(mcp_registry.decrypt_env(row.env_encrypted))
    return out


def _validate_shape(transport: str, command: str | None, url: str | None) -> None:
    if transport not in MCP_TRANSPORTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"BAD_TRANSPORT: {transport}")
    if transport == "stdio" and not command:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="STDIO_NEEDS_COMMAND")
    if transport == "http" and not url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="HTTP_NEEDS_URL")


@router.get("", response_model=list[McpServerRead])
async def list_servers(
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> list[McpServerRead]:
    rows = await session.execute(select(McpServer).order_by(McpServer.slug))
    return [_read(row) for row in rows.scalars().all()]


@router.post("", response_model=McpServerRead, status_code=status.HTTP_201_CREATED)
async def create_server(
    data: McpServerCreate,
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> McpServerRead:
    _validate_shape(data.transport, data.command, data.url)
    existing = await session.scalar(select(McpServer).where(McpServer.slug == data.slug))
    if existing is not None:
        # slug 是工具命名空间的一段，重名会让两台服务器的工具互相顶掉
        raise HTTPException(status.HTTP_409_CONFLICT, detail="SLUG_TAKEN")

    row = McpServer(
        slug=data.slug,
        title=data.title or data.slug,
        transport=data.transport,
        command=data.command,
        args=list(data.args or []),
        cwd=data.cwd,
        url=data.url,
        env_encrypted=mcp_registry.encrypt_env(data.env),
        enabled=data.enabled,
    )
    session.add(row)
    await session.flush()
    if row.enabled:
        await mcp_registry.sync_server(session, row)
    await session.commit()
    return _read(row)


async def _get(session: AsyncSession, server_id: uuid.UUID) -> McpServer:
    row = await session.get(McpServer, server_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="MCP_SERVER_NOT_FOUND")
    return row


@router.patch("/{server_id}", response_model=McpServerRead)
async def update_server(
    server_id: uuid.UUID,
    data: McpServerUpdate,
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> McpServerRead:
    row = await _get(session, server_id)
    fields = data.model_dump(exclude_unset=True)
    for key in ("title", "command", "cwd", "url"):
        if key in fields:
            setattr(row, key, fields[key])
    if "args" in fields:
        row.args = list(fields["args"] or [])
    if "env" in fields:
        row.env_encrypted = mcp_registry.encrypt_env(fields["env"])
    _validate_shape(row.transport, row.command, row.url)

    if "enabled" in fields:
        row.enabled = bool(fields["enabled"])
    # 配置变了就得重连：旧会话连的是旧命令/旧 env，留着它等于改了配置不生效
    await mcp_registry.forget_server(row)
    if row.enabled:
        await mcp_registry.sync_server(session, row)
    await session.commit()
    return _read(row)


@router.post("/{server_id}/sync", response_model=McpServerRead)
async def sync_server(
    server_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> McpServerRead:
    """手动重连并重新拉取工具。

    失败是**数据**（写进 last_error）而不是 500——外部软件连不上是最常见的情形，
    管理面要就地显示原因。
    """
    row = await _get(session, server_id)
    if not row.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="SERVER_DISABLED")
    await mcp_registry.forget_server(row)
    await mcp_registry.sync_server(session, row)
    await session.commit()
    return _read(row)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> None:
    row = await _get(session, server_id)
    await mcp_registry.forget_server(row)
    await session.delete(row)
    await session.commit()
