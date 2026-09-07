"""连接凭据（多态，R2 #677）：ssh_credentials 泛化为 connection_credentials。

为什么选「原表改造」而不是「新表 + 兼容视图」：SSHCredential 有 12 个消费方
（ssh_exec / experiments / voyage runner …）全部按列名直接取值，experiments 还有
指向本表的 FK。原表加 kind 列 + 表名改名，存量行零搬迁、消费方零改动（类名留
别名）；新表方案反而要么双写、要么把旧表做成视图（SQLAlchemy 写视图一堆坑）。

kind 与加密载荷约定（payload_encrypted = Fernet(json.dumps(payload))，见
app/core/security.py；所有敏感字段绝不明文出库/出 API）：

- ``ssh``：不用 payload_encrypted。沿用专列 host/port/username/
  private_key_encrypted/passphrase_encrypted/proxy_url——保持 ssh_exec 等
  消费方零改动，存量行即天然合法（kind 默认 'ssh'）。
- ``grpc``：host/port = gRPC endpoint；payload = {"token"?: str,
  "tls_ca"?: str(PEM 文本)}。
- ``http``：host/port = 基地址（路径等非敏感部分放 Resource.config）；
  payload = {"token"?: str, "username"?: str, "password"?: str,
  "headers"?: dict}。
- ``visa``：host = 仪器主机（展示用）；payload = {"resource": str
  (pyvisa 资源串，如 "TCPIP0::10.0.0.5::INSTR"), "secret"?: str}。
  资源串含内网定位信息，整体按敏感处理。
- ``ws``：BYO runner tier-2 机器凭据（#695）。连接方向反过来——agent 出站
  连回平台，host/port 仅作展示；payload = {"secret_sha256": str}，只存 agent
  secret 的 sha256 摘要（服务端只需验证、永不回读明文，泄库也拼不回 secret）。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin

# 合法凭据类型（Runner v2 manifest.credential_kinds 的运行时对应物）
CREDENTIAL_KINDS = ("ssh", "grpc", "visa", "http", "ws")


class ConnectionCredential(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "connection_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # ssh | grpc | visa | http | ws（载荷约定见模块 docstring）
    kind: Mapped[str] = mapped_column(String(16), default="ssh", nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    # ssh 必填；其他 kind 可空（http basic 的用户名放 payload，避免语义分叉）
    username: Mapped[str | None] = mapped_column(String(255))
    # ssh 专列：Fernet 加密（app/core/security.py），绝不以明文出库/出 API
    private_key_encrypted: Mapped[str | None] = mapped_column(Text)
    passphrase_encrypted: Mapped[str | None] = mapped_column(Text)
    # 非 ssh 的加密载荷：Fernet(json.dumps(payload))，字段约定见模块 docstring
    payload_encrypted: Mapped[str | None] = mapped_column(Text)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 服务器出外网需要的 HTTP 代理（如 http://10.205.70.120:7899），可空=直连；
    # 注入实验环境 http(s)_proxy，并对内网 LLM 地址设 no_proxy
    proxy_url: Mapped[str | None] = mapped_column(String(255))


# 兼容别名：存量消费方（ssh_exec / experiments / voyage runner / 测试）继续用旧名。
# 它们只走 kind='ssh' 的行，列名与语义完全不变。
SSHCredential = ConnectionCredential
