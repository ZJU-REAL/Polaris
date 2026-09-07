"""资源与连接凭据 schema（R2 #677）。凭据 Read 模型绝不含任何密钥字段。"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ResourceKind = Literal["host", "queue", "license_pool", "instrument"]
CredentialKind = Literal["ssh", "grpc", "visa", "http", "ws"]


class ResourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: ResourceKind
    capacity: int = Field(default=1, ge=1)
    exclusive: bool = True
    credential_id: uuid.UUID | None = None
    # kind 特定的非敏感配置；必填键在服务层校验（queue 要 queue 名、
    # license_pool 要 feature），报错才能带上 kind 上下文
    config: dict[str, Any] | None = None


class ResourceUpdate(BaseModel):
    """PATCH 语义：只更新给了的字段（kind 不可改——改类型等于换资源，重建去）。"""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    capacity: int | None = Field(default=None, ge=1)
    exclusive: bool | None = None
    credential_id: uuid.UUID | None = None
    config: dict[str, Any] | None = None


class ResourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: str
    capacity: int
    exclusive: bool
    credential_id: uuid.UUID | None
    config: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class RunnerHostRegister(BaseModel):
    """注册 BYO runner 主机（#685）：一台机器 = host 类 Resource + SSH 凭据关联。

    ephemeral 默认 True = 推荐容器化执行不残留；显式 False = 接受裸机直跑
    （non-ephemeral，产物留在主机）。只影响新注册的主机。"""

    name: str = Field(min_length=1, max_length=255)
    credential_id: uuid.UUID
    ephemeral: bool = True
    # 其余非敏感配置（workdir/gpus 等，语义见 app/models/resource.py）
    config: dict[str, Any] | None = None


class ConnectionCredentialCreate(BaseModel):
    """通用凭据创建（各 kind 载荷约定见 app/models/ssh_credential.py docstring）。

    ssh 也可以走这里（payload.private_key 必填，落存量专列）；但现有前端
    继续用 /ssh-credentials 老端点，两条路殊途同归。
    """

    name: str = Field(min_length=1, max_length=255)
    kind: CredentialKind
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    # 敏感载荷（整体 Fernet 加密入库，绝不回传）
    payload: dict[str, Any] = Field(default_factory=dict)
    proxy_url: str | None = Field(default=None, pattern=r"^https?://[A-Za-z0-9.\-]+(:\d+)?$")


class ConnectionCredentialRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    name: str
    host: str
    port: int
    username: str | None
    created_at: datetime
    last_verified_at: datetime | None
    proxy_url: str | None


# ---- BYO runner tier-2：出站 WebSocket agent 注册（#695） ----


class RunnerRegistrationTokenRead(BaseModel):
    """注册 token（一次性、短时效）：Web 端签发，agent 用它换长期机器凭据。"""

    token: str
    expires_in: int


class RunnerAgentRegister(BaseModel):
    """agent 侧注册请求：token 即鉴权（无需登录态——agent 机器上没有用户会话）。"""

    token: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    # agent 自述的机器信息（hostname/os/cpu 等，probe 结果），原样进 config.machine
    machine: dict[str, Any] | None = None


class RunnerAgentRegistered(BaseModel):
    """注册成功响应。agent_secret 仅此一次返回，服务端只存摘要、无法找回。"""

    resource: ResourceRead
    agent_secret: str
    # agent 出站连接的 WS 路径（相对服务器根，不挂 /api 前缀）
    ws_path: str
