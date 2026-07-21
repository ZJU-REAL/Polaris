"""用户自管 LLM 配置：取消/切回管理员接管，管理自己的 provider + 模型路由表。

被接管（llm_self_managed=False）时用全局(admin)配置，这里的私有配置不生效；
自管（True）时 resolve 用 owner=user 的配置，admin 的对他失效（见 core/llm/router.py）。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.llm.router import _FALLBACK_ROUTE, get_llm_router
from app.models.llm_config import LLMProviderConfig
from app.models.user import User
from app.schemas.llm_admin import (
    EffectiveTestRequest,
    EffectiveTestResult,
    LlmManagedStatus,
    LlmSelfConfig,
    ProviderCreate,
    ProviderRead,
    ProviderUpdate,
    RouteItem,
    TestModelRequest,
    TestModelResult,
)
from app.services import llm_admin as svc

_CAPABILITY_BY_STAGE = {"embedding": "embedding", "rerank": "rerank"}

router = APIRouter(prefix="/me/llm", tags=["me-llm"])


def _provider_read(p: LLMProviderConfig) -> ProviderRead:
    return ProviderRead(
        id=p.id,
        name=p.name,
        kind=p.kind,
        base_url=p.base_url,
        api_key_masked=svc.masked_key_of(p),
        enabled=p.enabled,
        models=p.models,
    )


async def _own_provider_or_404(
    session: AsyncSession, provider_id: uuid.UUID, user: User
) -> LLMProviderConfig:
    provider = await svc.get_provider(session, provider_id, owner_id=user.id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROVIDER_NOT_FOUND")
    return provider


# ---- 接管状态 ----


@router.get("/status", response_model=LlmManagedStatus)
async def get_status(user: User = Depends(current_active_user)) -> LlmManagedStatus:
    return LlmManagedStatus(self_managed=user.llm_self_managed)


@router.post("/self-manage", response_model=LlmManagedStatus)
async def switch_self_managed(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LlmManagedStatus:
    """取消接管：改用自己的配置（初始为空，需自行设置，配好前回退 fake）。"""
    user.llm_self_managed = True
    await session.commit()
    get_llm_router().invalidate_cache()
    return LlmManagedStatus(self_managed=True)


@router.post("/managed", response_model=LlmManagedStatus)
async def switch_managed(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LlmManagedStatus:
    """切回接管：改用管理员的全局配置（自己的私有配置保留但不生效）。"""
    user.llm_self_managed = False
    await session.commit()
    get_llm_router().invalidate_cache()
    return LlmManagedStatus(self_managed=False)


@router.get("/effective", response_model=LlmSelfConfig)
async def get_effective(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LlmSelfConfig:
    """当前生效配置（自管→自己的；被接管→全局 admin 的），只读展示用，key 掩码。"""
    owner = user.id if user.llm_self_managed else None
    providers = await svc.list_providers(session, owner_id=owner)
    routes = await svc.list_routes(session, owner_id=owner)
    return LlmSelfConfig(
        self_managed=user.llm_self_managed,
        providers=[_provider_read(p) for p in providers],
        routes=[RouteItem.model_validate(r, from_attributes=True) for r in routes],
    )


# ---- 自己的 providers ----


@router.get("/providers", response_model=list[ProviderRead])
async def list_own_providers(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ProviderRead]:
    return [_provider_read(p) for p in await svc.list_providers(session, owner_id=user.id)]


@router.post("/providers", response_model=ProviderRead, status_code=status.HTTP_201_CREATED)
async def create_own_provider(
    data: ProviderCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ProviderRead:
    try:
        provider = await svc.create_provider(session, data, owner_id=user.id)
    except IntegrityError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="PROVIDER_NAME_EXISTS") from e
    return _provider_read(provider)


@router.patch("/providers/{provider_id}", response_model=ProviderRead)
async def update_own_provider(
    provider_id: uuid.UUID,
    data: ProviderUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ProviderRead:
    provider = await _own_provider_or_404(session, provider_id, user)
    provider = await svc.update_provider(session, provider, data)
    return _provider_read(provider)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_own_provider(
    provider_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    provider = await _own_provider_or_404(session, provider_id, user)
    await svc.delete_provider(session, provider)


# ---- 自己的路由表 ----


@router.get("/routes", response_model=list[RouteItem])
async def list_own_routes(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[RouteItem]:
    routes = await svc.list_routes(session, owner_id=user.id)
    return [RouteItem.model_validate(r, from_attributes=True) for r in routes]


@router.put("/routes", response_model=list[RouteItem])
async def replace_own_routes(
    items: list[RouteItem],
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[RouteItem]:
    try:
        routes = await svc.replace_routes(session, items, owner_id=user.id)
    except svc.InvalidRouteError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return [RouteItem.model_validate(r, from_attributes=True) for r in routes]


@router.post("/test-model", response_model=TestModelResult)
async def test_own_model(
    data: TestModelRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> TestModelResult:
    provider = await _own_provider_or_404(session, data.provider_id, user)
    ok, latency_ms, error = await svc.test_model(provider, data.model, data.capability)
    return TestModelResult(ok=ok, latency_ms=latency_ms, error=error)


@router.post("/test-effective", response_model=EffectiveTestResult)
async def test_effective(
    data: EffectiveTestRequest,
    user: User = Depends(current_active_user),
) -> EffectiveTestResult:
    """测试当前生效配置：按 stage 探测用户实际会用到的 provider+model（含被接管的全局配置）。"""
    router_ = get_llm_router()
    llm, route = await router_.resolve(data.stage, user_id=user.id)
    if route is _FALLBACK_ROUTE:
        # 未配置真实模型（自管但没配好 / 全局也空）→ 回退到内置 fake
        return EffectiveTestResult(
            ok=False,
            latency_ms=0,
            error="NO_REAL_MODEL",
            model=route.model,
            provider_name=route.provider_name,
            is_fake=True,
        )
    capability = _CAPABILITY_BY_STAGE.get(data.stage, "chat")
    ok, latency_ms, error = await svc.probe_model(llm, route.model, capability)
    return EffectiveTestResult(
        ok=ok,
        latency_ms=latency_ms,
        error=error,
        model=route.model,
        provider_name=route.provider_name,
    )
