"""外部 MCP 服务器的登记（#754）。

一条记录 = 一台可被 agent 调用的外部 MCP 服务器：CAD、网格、求解器、Blender 等。

## env 为什么整体加密

stdio 服务器常常要靠环境变量拿许可证或 API key（``LSDYNA_LICENSE``、
``ONSHAPE_API_KEY``）。这些值和 SSH 凭据同级，不能明文躺在 JSON 列里——备份、
日志、误导出都会把它们带走。沿用 connection_credentials 已有的约定
（``Fernet(json.dumps(payload))``，见 core/security.py）而不是另发明一套：
密钥轮换、导出脱敏这些事只需要在一个地方做对。

命令与参数**不加密**：它们是可审计的运行事实（这台服务器到底会执行什么），
藏起来只会让「这个 agent 能跑什么」变得不可复核。
"""

from typing import Any

from sqlalchemy import Boolean, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

#: 传输方式。stdio = 由我们作为子进程拉起；http = 连到已在跑的服务。
MCP_TRANSPORTS = ("stdio", "http")


class McpServer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("slug", name="uq_mcp_servers_slug"),)

    #: 工具命名空间里的那一段（``mcp:<slug>:<tool>``）；改名等于换掉一批工具名，
    #: 所以它是稳定标识而不是展示名。
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    transport: Mapped[str] = mapped_column(String(16), nullable=False, default="stdio")

    # stdio：拉起子进程用
    command: Mapped[str | None] = mapped_column(String(512))
    args: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    cwd: Mapped[str | None] = mapped_column(String(1024))
    #: Fernet(json.dumps({k: v}))；见模块头
    env_encrypted: Mapped[str | None] = mapped_column(Text)

    # http：连已在跑的服务
    url: Mapped[str | None] = mapped_column(String(1024))

    #: 停用 = 不连、不注册它的工具；保留配置以便随时启回来
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: 上次同步到的工具名（``mcp:<slug>:<tool>`` 全名），供管理面展示与诊断。
    #: 只是**记录**不是真相——真相是注册表，重启后由同步重建。
    last_tools: Mapped[list[Any] | None] = mapped_column(JSONVariant)
    last_error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return f"<McpServer {self.slug} {self.transport}>"


__all__ = ["MCP_TRANSPORTS", "McpServer"]
