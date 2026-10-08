"""外部 agent（ACP 后端）的管理面（#836）。

门槛是 **owner**，与外部 MCP 服务器同一条理由：登记一个 agent 等于声明「服务端进程
可以拉起这条命令」，那是在服务器上执行任意程序的能力。桌面档位下 owner 就是本人。

env 只进不出：写入时加密落库，读取时只回键名。
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models.acp_agent import AcpAgent
from app.models.user import User
from app.services.acp import registry as acp_registry
from app.services.acp.client import PERMISSION_POLICIES
from app.services.acp.templates import CUSTOM, TEMPLATES, detect, get_template
from app.services.mcp_hub.registry import decrypt_env, encrypt_env
from app.services.owner import require_owner

router = APIRouter(prefix="/acp-agents", tags=["acp-agents"])

SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
ENV_KEY_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


class AcpAgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    template: str
    command: str
    args: list[Any] | None
    permission_policy: str
    enabled: bool
    shared: bool
    last_probe: dict[str, Any] | None
    last_error: str | None
    last_probed_at: Any = None
    env_keys: list[str] = []
    #: 命令此刻能不能在 PATH 里找到（不拉起进程，只查文件）
    command_found: bool = False


class AcpAgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    name: str = Field(default="", max_length=128)
    template: str = Field(default=CUSTOM, max_length=32)
    #: 不给就用模板的命令/参数
    command: str | None = Field(default=None, max_length=512)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    permission_policy: str = "deny"
    enabled: bool = True
    shared: bool = False


class AcpAgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=128)
    command: str | None = Field(default=None, min_length=1, max_length=512)
    args: list[str] | None = None
    #: 不传 = 不动；传 {} = 清空（读不回明文，所以只能整体替换）
    env: dict[str, str] | None = None
    permission_policy: str | None = None
    enabled: bool | None = None
    shared: bool | None = None


def _read(row: AcpAgent) -> AcpAgentRead:
    out = AcpAgentRead.model_validate(row)
    out.env_keys = sorted(decrypt_env(row.env_encrypted))
    out.command_found = acp_registry.command_found(row)
    return out


def _check_policy(policy: str) -> None:
    if policy not in PERMISSION_POLICIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"BAD_PERMISSION_POLICY: {policy}")


def _check_env(env: dict[str, str] | None) -> None:
    import re

    for key in env or {}:
        if not re.match(ENV_KEY_PATTERN, key):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"BAD_ENV_KEY: {key}")


@router.get("/templates")
async def list_templates(_owner: User = Depends(require_owner)) -> list[dict[str, Any]]:
    """内置模板 + 这台机器上装没装（只查 PATH，不拉起进程）。"""
    return [{**t.as_dict(), **detect(t)} for t in TEMPLATES]


@router.get("", response_model=list[AcpAgentRead])
async def list_agents(
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> list[AcpAgentRead]:
    rows = await session.execute(select(AcpAgent).order_by(AcpAgent.created_at))
    return [_read(row) for row in rows.scalars().all()]


@router.post("", response_model=AcpAgentRead, status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: AcpAgentCreate,
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> AcpAgentRead:
    _check_policy(data.permission_policy)
    _check_env(data.env)
    template = get_template(data.template)
    if data.template != CUSTOM and template is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"UNKNOWN_TEMPLATE: {data.template}"
        )
    command = data.command or (template.command if template else None)
    if not command:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="COMMAND_REQUIRED")
    args = data.args if data.args is not None else list(template.args if template else ())
    if await session.scalar(select(AcpAgent).where(AcpAgent.slug == data.slug)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="SLUG_TAKEN")
    row = AcpAgent(
        slug=data.slug,
        name=data.name or (template.name if template else data.slug),
        template=data.template,
        command=command,
        args=list(args),
        env_encrypted=encrypt_env(data.env),
        permission_policy=data.permission_policy,
        enabled=data.enabled,
        shared=data.shared,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _read(row)


async def _get(session: AsyncSession, agent_id: uuid.UUID) -> AcpAgent:
    row = await session.get(AcpAgent, agent_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ACP_AGENT_NOT_FOUND")
    return row


@router.patch("/{agent_id}", response_model=AcpAgentRead)
async def update_agent(
    agent_id: uuid.UUID,
    data: AcpAgentUpdate,
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> AcpAgentRead:
    row = await _get(session, agent_id)
    fields = data.model_dump(exclude_unset=True)
    if "permission_policy" in fields:
        _check_policy(fields["permission_policy"])
        row.permission_policy = fields["permission_policy"]
    if "env" in fields:
        _check_env(fields["env"])
        row.env_encrypted = encrypt_env(fields["env"])
    for key in ("name", "command", "enabled", "shared"):
        if key in fields and fields[key] is not None:
            setattr(row, key, fields[key])
    if "args" in fields:
        row.args = list(fields["args"] or [])
    if {"command", "args", "env"} & fields.keys():
        # 拉起方式变了，上次的探测结果不再说明任何事
        row.last_probe = None
        row.last_error = None
        row.last_probed_at = None
    await session.commit()
    await session.refresh(row)
    from app.services.acp.pool import get_pool

    # 配置变了：已经开着的会话用的是旧命令/旧策略，必须关掉
    await get_pool().forget_agent(row.id)
    return _read(row)


@router.post("/{agent_id}/probe", response_model=AcpAgentRead)
async def probe_agent(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> AcpAgentRead:
    """真拉起一次、做完握手。失败是**数据**（写进 last_error），不是 500。"""
    row = await _get(session, agent_id)
    await acp_registry.probe(row)
    await session.commit()
    await session.refresh(row)
    return _read(row)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> None:
    row = await _get(session, agent_id)
    from app.services.acp.pool import get_pool

    await get_pool().forget_agent(row.id)
    await session.delete(row)
    await session.commit()
