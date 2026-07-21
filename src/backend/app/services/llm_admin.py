"""管理端 LLM 配置业务逻辑（不 import fastapi）。

api_key 只写不读：入库前 Fernet 加密，读出时仅返回掩码（如 "sk-...abcd"）。
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.router import STAGES, get_llm_router
from app.core.security import decrypt_secret, encrypt_secret
from app.models.base import utcnow
from app.models.llm_config import LLMProviderConfig, LLMUsage, ModelRoute
from app.schemas.llm_admin import ProviderCreate, ProviderUpdate, RouteItem


class InvalidRouteError(Exception):
    """路由表引用了非法 stage 或不存在的 provider。"""


def mask_api_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:3]}...{key[-4:]}"


def masked_key_of(provider: LLMProviderConfig) -> str:
    if not provider.api_key_encrypted:
        return ""
    return mask_api_key(decrypt_secret(provider.api_key_encrypted))


# ---- providers ----


async def list_providers(session: AsyncSession) -> Sequence[LLMProviderConfig]:
    stmt = select(LLMProviderConfig).order_by(LLMProviderConfig.created_at)
    return (await session.execute(stmt)).scalars().all()


async def get_provider(session: AsyncSession, provider_id: uuid.UUID) -> LLMProviderConfig | None:
    return await session.get(LLMProviderConfig, provider_id)


async def create_provider(session: AsyncSession, data: ProviderCreate) -> LLMProviderConfig:
    provider = LLMProviderConfig(
        name=data.name,
        kind=data.kind,
        base_url=data.base_url,
        api_key_encrypted=encrypt_secret(data.api_key) if data.api_key else None,
        enabled=data.enabled,
        models=data.models,
    )
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    get_llm_router().invalidate_cache()
    return provider


async def update_provider(
    session: AsyncSession, provider: LLMProviderConfig, data: ProviderUpdate
) -> LLMProviderConfig:
    if data.name is not None:
        provider.name = data.name
    if data.kind is not None:
        provider.kind = data.kind
    if data.base_url is not None:
        provider.base_url = data.base_url
    if data.api_key:  # 空字符串/None = 不变
        provider.api_key_encrypted = encrypt_secret(data.api_key)
    if data.enabled is not None:
        provider.enabled = data.enabled
    if data.models is not None:  # 整体替换；清空传 []
        provider.models = data.models
    await session.commit()
    await session.refresh(provider)
    get_llm_router().invalidate_cache()
    return provider


async def delete_provider(session: AsyncSession, provider: LLMProviderConfig) -> None:
    await session.delete(provider)
    await session.commit()
    get_llm_router().invalidate_cache()


# ---- routes ----


async def list_routes(session: AsyncSession) -> Sequence[ModelRoute]:
    stmt = select(ModelRoute).order_by(ModelRoute.stage)
    return (await session.execute(stmt)).scalars().all()


async def replace_routes(session: AsyncSession, items: Sequence[RouteItem]) -> Sequence[ModelRoute]:
    """整表覆盖路由。stage 必须合法且不重复，provider 必须存在。"""
    seen: set[str] = set()
    for item in items:
        if item.stage not in STAGES:
            raise InvalidRouteError(f"unknown stage: {item.stage}")
        if item.stage in seen:
            raise InvalidRouteError(f"duplicate stage: {item.stage}")
        seen.add(item.stage)
        if await session.get(LLMProviderConfig, item.provider_id) is None:
            raise InvalidRouteError(f"provider not found: {item.provider_id}")
    await session.execute(delete(ModelRoute))
    for item in items:
        session.add(
            ModelRoute(
                stage=item.stage,
                provider_id=item.provider_id,
                model=item.model,
                temperature=item.temperature,
            )
        )
    await session.commit()
    get_llm_router().invalidate_cache()
    return await list_routes(session)


# ---- usage ----


async def usage_report(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    days: int = 30,
) -> list[dict[str, Any]]:
    """按 日期 × stage × model 聚合最近 N 天的用量。"""
    since = utcnow() - timedelta(days=days)
    date_col = func.date(LLMUsage.created_at).label("date")
    stmt = (
        select(
            date_col,
            LLMUsage.stage,
            LLMUsage.model,
            func.sum(LLMUsage.prompt_tokens).label("prompt_tokens"),
            func.sum(LLMUsage.completion_tokens).label("completion_tokens"),
            func.count().label("calls"),
        )
        .where(LLMUsage.created_at >= since)
        .group_by(date_col, LLMUsage.stage, LLMUsage.model)
        .order_by(date_col.desc(), LLMUsage.stage, LLMUsage.model)
    )
    if project_id is not None:
        stmt = stmt.where(LLMUsage.project_id == project_id)
    if user_id is not None:
        stmt = stmt.where(LLMUsage.user_id == user_id)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "date": str(row.date),
            "stage": row.stage,
            "model": row.model,
            "prompt_tokens": int(row.prompt_tokens or 0),
            "completion_tokens": int(row.completion_tokens or 0),
            "calls": int(row.calls),
        }
        for row in rows
    ]
