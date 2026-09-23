"""LLM 配置路由（仅平台主人：桌面档=本人；服务器档=首位用户）。

接口清单见 docs/task-system.md §7（原 api-m1.md §2）。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.llm.budgets import CHARS_PER_WINDOW_TOKEN, INPUT_BUDGETS
from app.models.llm_config import LLMCallLog, LLMProviderConfig
from app.models.user import User
from app.schemas.llm_admin import (
    CallLogDetail,
    CallLogPage,
    CallLogRow,
    CallLogSettings,
    InputBudgetSpec,
    ProviderCreate,
    ProviderRead,
    ProviderUpdate,
    RouteItem,
    TestModelRequest,
    TestModelResult,
    UsageRow,
)
from app.services import llm_admin as llm_admin_service
from app.services.owner import is_owner, require_owner

router = APIRouter(prefix="/admin/llm", tags=["admin-llm"])


async def config_scope(
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_session),
) -> uuid.UUID | None:
    """这次请求该读写哪一张配置表。

    部署主人动部署级那张（``None`` → owner IS NULL），也就是所有人的回退底；
    其他人动自己那张。同一个设置页，谁打开就配谁的——公有云里第二个注册的人
    本来连页面都打不开（``require_owner``），而「登录后提示去配模型」这件事
    只有在他真的配得了的时候才不是一句空话（#801）。

    用量与调用日志不走这里：那两处看的是整个部署的账，仍然只对主人开放。
    """
    return None if await is_owner(session, user) else user.id


def _provider_read(provider: LLMProviderConfig) -> ProviderRead:
    return ProviderRead(
        id=provider.id,
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        user_agent=provider.user_agent,
        api_key_masked=llm_admin_service.masked_key_of(provider),
        enabled=provider.enabled,
        models=provider.models,
    )


async def _get_provider_or_404(
    session: AsyncSession, provider_id: uuid.UUID, owner_id: uuid.UUID | None = None
) -> LLMProviderConfig:
    provider = await llm_admin_service.get_provider(session, provider_id, owner_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROVIDER_NOT_FOUND")
    return provider


@router.get("/providers", response_model=list[ProviderRead])
async def list_providers(
    session: AsyncSession = Depends(get_session),
    scope: uuid.UUID | None = Depends(config_scope),
) -> list[ProviderRead]:
    providers = await llm_admin_service.list_providers(session, scope)
    return [_provider_read(p) for p in providers]


@router.post("/providers", response_model=ProviderRead, status_code=status.HTTP_201_CREATED)
async def create_provider(
    data: ProviderCreate,
    session: AsyncSession = Depends(get_session),
    scope: uuid.UUID | None = Depends(config_scope),
) -> ProviderRead:
    try:
        provider = await llm_admin_service.create_provider(session, data, scope)
    except IntegrityError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="PROVIDER_NAME_EXISTS") from e
    return _provider_read(provider)


@router.patch("/providers/{provider_id}", response_model=ProviderRead)
async def update_provider(
    provider_id: uuid.UUID,
    data: ProviderUpdate,
    session: AsyncSession = Depends(get_session),
    scope: uuid.UUID | None = Depends(config_scope),
) -> ProviderRead:
    provider = await _get_provider_or_404(session, provider_id, scope)
    provider = await llm_admin_service.update_provider(session, provider, data)
    return _provider_read(provider)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    scope: uuid.UUID | None = Depends(config_scope),
) -> None:
    provider = await _get_provider_or_404(session, provider_id, scope)
    await llm_admin_service.delete_provider(session, provider)


@router.get("/routes", response_model=list[RouteItem])
async def list_routes(
    session: AsyncSession = Depends(get_session),
    scope: uuid.UUID | None = Depends(config_scope),
) -> list[RouteItem]:
    routes = await llm_admin_service.list_routes(session, scope)
    return [RouteItem.model_validate(r, from_attributes=True) for r in routes]


@router.put("/routes", response_model=list[RouteItem])
async def replace_routes(
    items: list[RouteItem],
    session: AsyncSession = Depends(get_session),
    scope: uuid.UUID | None = Depends(config_scope),
) -> list[RouteItem]:
    try:
        routes = await llm_admin_service.replace_routes(session, items, scope)
    except llm_admin_service.InvalidRouteError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return [RouteItem.model_validate(r, from_attributes=True) for r in routes]


@router.get("/input-budgets", response_model=list[InputBudgetSpec])
async def list_input_budgets(
    _scope: uuid.UUID | None = Depends(config_scope),
) -> list[InputBudgetSpec]:
    """可调的输入预算清单（#811）。设置页照这张表画输入框，不在前端另抄一份。"""
    return [
        InputBudgetSpec(
            stage=b.stage,
            key=b.key,
            default=b.default,
            minimum=b.minimum,
            maximum=b.maximum,
            chars_per_window_token=CHARS_PER_WINDOW_TOKEN,
        )
        for b in INPUT_BUDGETS
    ]


@router.post("/test-model", response_model=TestModelResult)
async def test_model(
    data: TestModelRequest,
    session: AsyncSession = Depends(get_session),
    scope: uuid.UUID | None = Depends(config_scope),
) -> TestModelResult:
    """最小化探测 provider+model 连通性（不经过路由表，不记账、不写调用日志）。"""
    provider = await _get_provider_or_404(session, data.provider_id, scope)
    ok, latency_ms, error = await llm_admin_service.test_model(
        provider, data.model, data.capability
    )
    return TestModelResult(ok=ok, latency_ms=latency_ms, error=error)


@router.get("/usage", response_model=list[UsageRow])
async def usage_report(
    _owner: User = Depends(require_owner),
    project_id: uuid.UUID | None = Query(default=None),
    user_id: uuid.UUID | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
) -> list[UsageRow]:
    rows = await llm_admin_service.usage_report(
        session, project_id=project_id, user_id=user_id, days=days
    )
    return [UsageRow(**row) for row in rows]


# ---- 调用日志 ----

_PREVIEW_CHARS = 200


def _preview(text: str, limit: int = _PREVIEW_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _request_preview(request: object) -> str:
    """取最后一条消息内容的截断预览（embed/rerank 摘要取首条输入）。"""
    if not isinstance(request, dict):
        return ""
    messages = request.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            return _preview(str(last.get("content", "")))
    for key in ("first_text", "query"):
        if key in request:
            return _preview(str(request.get(key) or ""))
    return ""


def _call_log_row(log: LLMCallLog) -> CallLogRow:
    return CallLogRow(
        id=log.id,
        created_at=log.created_at,
        stage=log.stage,
        provider_name=log.provider_name,
        model=log.model,
        duration_ms=log.duration_ms,
        status=log.status,
        error=log.error,
        prompt_tokens=log.prompt_tokens,
        completion_tokens=log.completion_tokens,
        user_id=log.user_id,
        project_id=log.project_id,
        voyage_id=log.voyage_id,
        request_preview=_request_preview(log.request),
        response_preview=_preview(log.response or ""),
    )


@router.get("/call-logs/settings", response_model=CallLogSettings)
async def get_call_log_settings(
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> CallLogSettings:
    return CallLogSettings(enabled=await llm_admin_service.get_call_logging_enabled(session))


@router.put("/call-logs/settings", response_model=CallLogSettings)
async def put_call_log_settings(
    data: CallLogSettings,
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> CallLogSettings:
    enabled = await llm_admin_service.set_call_logging_enabled(session, data.enabled)
    return CallLogSettings(enabled=enabled)


@router.get("/call-logs", response_model=CallLogPage)
async def list_call_logs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    stage: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> CallLogPage:
    total, rows = await llm_admin_service.list_call_logs(
        session, stage=stage, limit=limit, offset=offset
    )
    return CallLogPage(total=total, items=[_call_log_row(r) for r in rows])


@router.delete("/call-logs")
async def clear_call_logs(
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> dict[str, int]:
    deleted = await llm_admin_service.clear_call_logs(session)
    return {"deleted": deleted}


@router.get("/call-logs/{log_id}", response_model=CallLogDetail)
async def get_call_log(
    log_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_owner),
) -> CallLogDetail:
    log = await llm_admin_service.get_call_log(session, log_id)
    if log is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CALL_LOG_NOT_FOUND")
    return CallLogDetail.model_validate(log, from_attributes=True)
