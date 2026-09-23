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

from cryptography.fernet import InvalidToken
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import call_log
from app.core.llm.anthropic import AnthropicProvider
from app.core.llm.base import LLMProvider, Message
from app.core.llm.budgets import validate_budgets
from app.core.llm.fake import FakeProvider
from app.core.llm.openai_compat import OpenAICompatProvider
from app.core.llm.router import get_llm_router, is_plugin_stage, known_stages
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
    try:
        return mask_api_key(decrypt_secret(provider.api_key_encrypted))
    except InvalidToken:
        # 这一条记录解不开（多半是加密密钥轮换过）→ 让设置页仍能打开、能重填。
        # 运行时构造 provider 走的是另一条 decrypt_secret，仍然严格 fail closed。
        #
        # 只接 InvalidToken，不接 ValueError：token 无论怎么坏，Fernet.decrypt 抛的都是
        # InvalidToken；真正会抛 ValueError 的是 Fernet(key) 本身——也就是服务端
        # POLARIS_ENCRYPTION_KEY 配错了。那是部署错误，不是某个 provider 的数据问题，
        # 接住它会让每个 provider 都显示"去重填 key"，而管理员照做时 encrypt_secret 会
        # 抛同一个 ValueError 报 500，唯一的线索却已经被吞掉了。
        return "*** (needs reconfiguration)"


def _normalize_user_agent(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


# ---- providers ----


# owner_id IS NULL 过滤不能省：自管轨退役（#621）只是不再提供入口，
# 老部署的表里可能还留着 owner=<user> 的存量私有行，混出来就是把某个用户的
# 私有 key/路由当成了平台配置。


def _owner_clause(column: Any, owner_id: uuid.UUID | None):
    """归属过滤：``None`` = 部署级那张表，否则 = 这个用户自己的那张。

    两边都是精确匹配。少了这层，一个用户的私有配置会出现在别人的设置页上。
    """
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
    """取这个归属下的 provider；别人的行一律按不存在处理。

    按 404 而不是 403 处理：告诉一个人「这个 id 存在但不归你」，本身就是在
    泄露别人配了什么。
    """
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
        user_agent=_normalize_user_agent(data.user_agent),
        api_key_encrypted=encrypt_secret(data.api_key) if data.api_key else None,
        enabled=data.enabled,
        models=data.models,
        rerank_path=data.rerank_path,
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
    if data.user_agent is not None:
        provider.user_agent = _normalize_user_agent(data.user_agent)
    if data.api_key:  # 空字符串/None = 不变
        provider.api_key_encrypted = encrypt_secret(data.api_key)
    if data.enabled is not None:
        provider.enabled = data.enabled
    if data.models is not None:  # 整体替换；清空传 []
        provider.models = data.models
    if data.rerank_path is not None:
        provider.rerank_path = data.rerank_path
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
    """整表覆盖这个归属的路由。stage 必须合法且不重复，provider 必须同属一人。"""
    seen: set[str] = set()
    valid_stages = known_stages()
    for item in items:
        # 内置环节必须精确命中；插件命名空间串（plugin:<pack>:<stage>）按形状放行，
        # 即使对应插件此刻没加载——PUT 是整表覆盖，若插件卸载后就不认它的存量
        # 路由行，管理员改任何一个环节都会 400，整张表从此存不进去（digest 的
        # 事故换个马甲重演）。没人认领的插件路由行只是躺着不被用，无害。
        if item.stage not in valid_stages and not is_plugin_stage(item.stage):
            raise InvalidRouteError(f"unknown stage: {item.stage}")
        if item.stage in seen:
            raise InvalidRouteError(f"duplicate stage: {item.stage}")
        seen.add(item.stage)
        # provider 必须与路由同属一人：否则可以把别人的 provider id 写进
        # 自己的路由表，拿别人的 key 跑自己的任务。
        provider = await session.get(LLMProviderConfig, item.provider_id)
        if provider is None or provider.owner_id != owner_id:
            raise InvalidRouteError(f"provider not found: {item.provider_id}")
        try:
            validate_budgets(item.stage, item.input_budgets, item.context_window)
        except ValueError as e:
            raise InvalidRouteError(str(e)) from e
    await session.execute(
        delete(ModelRoute).where(_owner_clause(ModelRoute.owner_id, owner_id))
    )
    for item in items:
        session.add(
            ModelRoute(
                owner_id=owner_id,
                stage=item.stage,
                provider_id=item.provider_id,
                model=item.model,
                temperature=item.temperature,
                effort=item.effort,
                context_window=item.context_window,
                # 空字典存成 NULL：两者含义相同（全用默认），只留一种写法
                input_budgets=item.input_budgets or None,
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
        # rerank 路径也要带上：「测试连接」验的就是这份配置，漏了它，改成 /reranks 的
        # 服务在这里照样打 /rerank，测出来的是另一回事
        return OpenAICompatProvider(
            base_url=base_url, api_key=api_key, rerank_path=provider.rerank_path
        )
    if provider.kind == "openai_responses":
        from app.core.config import get_settings
        from app.core.llm.openai_responses import OpenAIResponsesProvider

        base_url = provider.base_url or get_settings().openai_compat_base_url
        # rerank 继承自 compat provider，路径同样要带上
        return OpenAIResponsesProvider(
            base_url=base_url, api_key=api_key, rerank_path=provider.rerank_path
        )
    if provider.kind == "anthropic":
        return AnthropicProvider(
            api_key=api_key,
            base_url=provider.base_url,
            user_agent=provider.user_agent,
        )
    if provider.kind == "fake":
        return FakeProvider()
    # 以前这里对任何没认出来的类型都回退 FakeProvider——于是新加一种协议而忘了改
    # 这里，「测试连接」就会去探一个假 provider 并报告成功，真实服务一次都没碰过。
    # 宁可报错。
    raise ValueError(f"unknown LLM provider kind: {provider.kind!r}")


async def probe_model(
    llm: LLMProvider, model: str, capability: str
) -> tuple[bool, int, str | None]:
    """用一个已构建的 provider 实例最小化探测 model，返回 (ok, latency_ms, error)。

    不 aclose（可能是路由器缓存的共享实例）；一次性 provider 的清理由调用方负责。
    """
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
    except Exception as e:  # noqa: BLE001 — 探测失败原因原样返回
        error = f"{type(e).__name__}: {e}"
    latency_ms = max(1, int((time.monotonic() - started) * 1000))
    return ok, latency_ms, error


async def test_model(
    provider: LLMProviderConfig, model: str, capability: str
) -> tuple[bool, int, str | None]:
    """按 capability 最小化探测一个 provider+model 组合（直连，绕过路由/记账）。"""
    llm = _build_provider(provider)
    try:
        return await probe_model(llm, model, capability)
    finally:
        aclose = getattr(llm, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):  # 清理失败不影响探测结果
                await aclose()


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
