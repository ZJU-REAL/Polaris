"""实验资源与租约（R2 #677，设计报告 §14）。

Resource 是 Runner v2 manifest 里 resources/licenses 声明的运行时对应物：
后端说「我要一台主机 / 一个 License 席位」，平台在这张表里找对应实体并发租约。

- ``kind`` 四档：host（SSH 主机）| queue（集群队列）| license_pool（FlexLM 等
  License 池）| instrument（实体仪器）。
- ``exclusive``：独占资源（仪器/整机）同一时刻只允许一个活租约，此时 capacity
  语义上恒为 1；非独占资源按 capacity 计数（License 池的席位数、队列的并发额）。
- ``config`` 放 kind 特定的非敏感字段（敏感的进 ConnectionCredential.payload）：
  host: {"workdir"?, "gpus"?}；queue: {"queue": 队列名, "partition"?}；
  license_pool: {"feature": License feature 名, "server"?: "host:port"}；
  instrument: {"resource"?: pyvisa 资源串（不敏感时）, "location"?}。

ResourceLease 一行 = 一次「某 run 占用某资源」：released_at 为空即活租约。
真正的持久化排队表（排队席位可见、跨进程公平）归后续 PR，本期 wait 语义用
轮询实现（app/services/resource_leases.py）。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin, utcnow

RESOURCE_KINDS = ("host", "queue", "license_pool", "instrument")


class Resource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "resources"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # host | queue | license_pool | instrument
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # 非独占资源的并发席位数（exclusive=True 时忽略，按 1 算）
    capacity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    exclusive: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 连接凭据（多态）；凭据删除不连坐资源（SET NULL），用时再校验
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connection_credentials.id", ondelete="SET NULL")
    )
    # kind 特定的非敏感配置，字段约定见模块 docstring
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)


class ResourceLease(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "resource_leases"

    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), index=True, nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("voyage_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    # 空 = 活租约。释放三条路：prepare/cleanup 显式释放、run 终态兜底、cancel 兜底
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(String(255), default="", nullable=False)
