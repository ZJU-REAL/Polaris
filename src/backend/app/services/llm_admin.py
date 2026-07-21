"""管理端 LLM 配置业务逻辑（不 import fastapi）。

api_key 只写不读：入库前 Fernet 加密，读出时仅返回掩码（如 "sk-...abcd"）。
"""

import asyncio
import contextlib
import time
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import call_log
from app.core.llm.anthropic import AnthropicProvider
from app.core.llm.base import LLMProvider, Message
from app.core.llm.fake import FakeProvider
from app.core.llm.openai_compat import OpenAICompatProvider
from app.core.llm.router import STAGES, get_llm_router
from app.core.security import decrypt_secret, encrypt_secret
from app.models.base import utcnow
from app.models.llm_config import LLMCallLog, LLMProviderConfig, LLMUsage, ModelRoute
from app.models.system_setting import SystemSetting
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


def _owner_clause(column, owner_id: uuid.UUID | None):
    """owner 过滤：None → 全局(IS NULL)；<user> → 该用户。"""
    return column.is_(None) if owner_id is None else column == owner_id


async def list_providers(
    session: AsyncSession, owner_id: uuid.UUID | None = None
) -> Sequence[LLMProviderConfig]:
    stmt = (
        select(LLMProviderConfig)
        .where(_owner_clause(LLMProviderConfig.owner_id, owner_id))
        .order_by(LLMProviderConfig.created_at)
    )
    return (await session.execute(stmt)).scalars().all()


async def get_provider(
    session: AsyncSession, provider_id: uuid.UUID, owner_id: uuid.UUID | None = None
) -> LLMProviderConfig | None:
    """按 owner 取 provider：owner 不匹配返回 None（防跨 owner 改别人配置）。"""
    provider = await session.get(LLMProviderConfig, provider_id)
    if provider is None or provider.owner_id != owner_id:
        return None
    return provider


async def create_provider(
    session: AsyncSession, data: ProviderCreate, owner_id: uuid.UUID | None = None
) -> LLMProviderConfig:
    provider = LLMProviderConfig(
        owner_id=owner_id,
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


async def list_routes(
    session: AsyncSession, owner_id: uuid.UUID | None = None
) -> Sequence[ModelRoute]:
    stmt = (
        select(ModelRoute)
        .where(_owner_clause(ModelRoute.owner_id, owner_id))
        .order_by(ModelRoute.stage)
    )
    return (await session.execute(stmt)).scalars().all()


async def replace_routes(
    session: AsyncSession, items: Sequence[RouteItem], owner_id: uuid.UUID | None = None
) -> Sequence[ModelRoute]:
    """整表覆盖某 owner 的路由。stage 必须合法且不重复，provider 必须属于同一 owner。"""
    seen: set[str] = set()
    for item in items:
        if item.stage not in STAGES:
            raise InvalidRouteError(f"unknown stage: {item.stage}")
        if item.stage in seen:
            raise InvalidRouteError(f"duplicate stage: {item.stage}")
        seen.add(item.stage)
        provider = await session.get(LLMProviderConfig, item.provider_id)
        if provider is None or provider.owner_id != owner_id:
            raise InvalidRouteError(f"provider not found: {item.provider_id}")
    await session.execute(delete(ModelRoute).where(_owner_clause(ModelRoute.owner_id, owner_id)))
    for item in items:
        session.add(
            ModelRoute(
                owner_id=owner_id,
                stage=item.stage,
                provider_id=item.provider_id,
                model=item.model,
                temperature=item.temperature,
            )
        )
    await session.commit()
    get_llm_router().invalidate_cache()
    return await list_routes(session, owner_id)


# ---- 模型连通性测试 ----

_TEST_TIMEOUT_S = 20.0


def _build_provider(provider: LLMProviderConfig) -> LLMProvider:
    """按 provider 配置直接构造实例（不经过路由表；openai_compat 天然带强制流式回退）。"""
    api_key = decrypt_secret(provider.api_key_encrypted) if provider.api_key_encrypted else ""
    if provider.kind == "openai_compat":
        from app.core.config import get_settings

        base_url = provider.base_url or get_settings().openai_compat_base_url
        return OpenAICompatProvider(base_url=base_url, api_key=api_key)
    if provider.kind == "anthropic":
        return AnthropicProvider(api_key=api_key)
    return FakeProvider()


async def test_model(
    provider: LLMProviderConfig, model: str, capability: str
) -> tuple[bool, int, str | None]:
    """按 capability 最小化探测一个 provider+model 组合。

    直连 provider（绕过 LLMRouter），因此不写 LLMUsage 记账、不写调用日志。
    返回 (ok, latency_ms, error)。
    """
    llm = _build_provider(provider)
    started = time.monotonic()
    ok, error = False, None
    try:
        async with asyncio.timeout(_TEST_TIMEOUT_S):
            if capability == "embedding":
                await llm.embed(["ping"], model=model)
            elif capability == "rerank":
                await llm.rerank("ping", ["ping"], model=model)
            else:
                messages = [Message(role="user", content="ping")]
                await llm.complete(messages, model=model, max_tokens=8)
        ok = True
    except TimeoutError:
        error = f"timeout after {_TEST_TIMEOUT_S:.0f}s"
    except Exception as e:  # noqa: BLE001 — 探测失败原因原样返回给管理端
        error = f"{type(e).__name__}: {e}"
    finally:
        aclose = getattr(llm, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):  # 清理失败不影响探测结果
                await aclose()
    latency_ms = max(1, int((time.monotonic() - started) * 1000))
    return ok, latency_ms, error


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


# ---- 调用日志 ----


async def get_call_logging_enabled(session: AsyncSession) -> bool:
    """调用日志开关（system_settings 表，默认关）。"""
    row = await session.get(SystemSetting, call_log.LLM_CALL_LOGGING_KEY)
    return bool(row.value) if row is not None else False


async def set_call_logging_enabled(session: AsyncSession, enabled: bool) -> bool:
    row = await session.get(SystemSetting, call_log.LLM_CALL_LOGGING_KEY)
    if row is None:
        session.add(SystemSetting(key=call_log.LLM_CALL_LOGGING_KEY, value=enabled))
    else:
        row.value = enabled
    await session.commit()
    call_log.invalidate_flag_cache()  # 免重启即生效
    return enabled


async def list_call_logs(
    session: AsyncSession,
    *,
    stage: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, Sequence[LLMCallLog]]:
    """时间倒序分页；返回 (总数, 当页行)。"""
    where = [LLMCallLog.stage == stage] if stage else []
    total = (
        await session.execute(select(func.count()).select_from(LLMCallLog).where(*where))
    ).scalar_one()
    stmt = (
        select(LLMCallLog)
        .where(*where)
        .order_by(LLMCallLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return int(total), rows


async def get_call_log(session: AsyncSession, log_id: uuid.UUID) -> LLMCallLog | None:
    return await session.get(LLMCallLog, log_id)


async def clear_call_logs(session: AsyncSession) -> int:
    result = await session.execute(delete(LLMCallLog))
    await session.commit()
    return int(result.rowcount or 0)
