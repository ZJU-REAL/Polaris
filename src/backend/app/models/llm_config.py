"""LLM 配置与记账：provider 凭据（Fernet 加密）、环节路由表、用量流水、调用日志。"""

import uuid
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class LLMProviderConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "llm_providers"
    # name 按 owner 分别唯一（全局 owner NULL 与每个用户各自唯一）
    __table_args__ = (
        Index(
            "uq_providers_global_name",
            "name",
            unique=True,
            sqlite_where=text("owner_id IS NULL"),
            postgresql_where=text("owner_id IS NULL"),
        ),
        Index(
            "uq_providers_owner_name",
            "owner_id",
            "name",
            unique=True,
            sqlite_where=text("owner_id IS NOT NULL"),
            postgresql_where=text("owner_id IS NOT NULL"),
        ),
    )

    # 归属：NULL = 平台全局（管理员管）；<user> = 该用户自管的私有 provider。
    # 唯一性按 owner 分别约束（见迁移的两条部分唯一索引），不再全局唯一。
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # openai_compat|anthropic|fake
    base_url: Mapped[str | None] = mapped_column(String(1024))
    # 明文 key 不落库：core/security.py Fernet 加密后存这里
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 该 provider 可用的模型 id 列表（字符串数组；None = 未配置，前端不给候选）
    models: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    routes: Mapped[list["ModelRoute"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )


class ModelRoute(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "model_routes"
    # stage 按 owner 分别唯一
    __table_args__ = (
        Index(
            "uq_routes_global_stage",
            "stage",
            unique=True,
            sqlite_where=text("owner_id IS NULL"),
            postgresql_where=text("owner_id IS NULL"),
        ),
        Index(
            "uq_routes_owner_stage",
            "owner_id",
            "stage",
            unique=True,
            sqlite_where=text("owner_id IS NOT NULL"),
            postgresql_where=text("owner_id IS NOT NULL"),
        ),
    )

    # 归属：NULL = 全局（管理员）；<user> = 该用户自管。每个 owner 的每个 stage 至多一条。
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # 科研环节，见 core/llm/router.py STAGES
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("llm_providers.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)  # None=不传该参数

    provider: Mapped[LLMProviderConfig] = relationship(back_populates="routes")


class LLMUsage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "llm_usage"

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    # 方向库归因（P6）：库侧 ingest/打分/编译/概念定义/向量化记库账，个人消费为 NULL
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="SET NULL"), index=True
    )
    voyage_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voyage_runs.id", ondelete="SET NULL")
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class LLMCallLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """LLM 调用日志（管理端开关打开时记录；仅保留最近 7 天）。

    request 为脱敏后的 JSON：messages 数组（超长内容截断），图片绝不存 base64，
    只留 "[image ~N KB]" 占位；response 为完整输出文本（超长截断）。
    """

    __tablename__ = "llm_call_logs"
    __table_args__ = (Index("ix_llm_call_logs_created_at", "created_at"),)

    stage: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)  # ok|error
    error: Mapped[str | None] = mapped_column(Text)
    request: Mapped[Any | None] = mapped_column(JSONVariant, nullable=True)
    response: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL")
    )
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("direction_libraries.id", ondelete="SET NULL")
    )
    voyage_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voyage_runs.id", ondelete="SET NULL")
    )
