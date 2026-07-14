"""LLM 配置与记账：provider 凭据（Fernet 加密）、环节路由表、用量流水。"""

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class LLMProviderConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "llm_providers"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # openai_compat|anthropic|fake
    base_url: Mapped[str | None] = mapped_column(String(1024))
    # 明文 key 不落库：core/security.py Fernet 加密后存这里
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    routes: Mapped[list["ModelRoute"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )


class ModelRoute(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "model_routes"

    # 科研环节，见 core/llm/router.py STAGES；每个 stage 至多一条路由
    stage: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
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
    voyage_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voyage_runs.id", ondelete="SET NULL")
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
