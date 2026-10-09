"""登记过的外部 agent（ACP 后端，#836）。

一条记录 = 一个可以替 Polaris 干活的外部 agent：Claude Code、Codex、Gemini CLI……
由服务端进程按 command/args/env 拉起，经 ACP 对话。

与外部 MCP 服务器（models/mcp_server）同一套约定：env 整体加密、读取时永不回传；
命令和参数不加密——「这台机器会替 agent 执行什么」必须可复核。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class AcpAgent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "acp_agents"
    __table_args__ = (UniqueConstraint("slug", name="uq_acp_agents_slug"),)

    #: 稳定标识（会话里记的是它），改名不影响已有对话
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    #: 从哪个内置模板来（services/acp/templates）；自定义为 "custom"
    template: Mapped[str] = mapped_column(String(32), nullable=False, default="custom")

    command: Mapped[str] = mapped_column(String(512), nullable=False)
    args: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    #: Fernet(json.dumps({k: v}))；见 services/mcp_hub/registry.encrypt_env
    env_encrypted: Mapped[str | None] = mapped_column(Text)

    #: deny / read_only / auto（见 services/acp/client 的模块头）
    permission_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="deny")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: 上次探测（握手）拿到的 agent 自述；失败时为 None、原因在 last_error
    last_probe: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return f"<AcpAgent {self.slug} {self.command}>"


__all__ = ["AcpAgent"]
