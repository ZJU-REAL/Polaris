"""管理端 LLM 配置路由（仅 role=admin，docs/api-m1.md §2）。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.core.db import get_session
from app.models.llm_config import LLMProviderConfig
from app.schemas.llm_admin import (
    ProviderCreate,
    ProviderRead,
    ProviderUpdate,
    RouteItem,
    UsageRow,
)
from app.services import llm_admin as llm_admin_service

router = APIRouter(prefix="/admin/llm", tags=["admin-llm"], dependencies=[Depends(require_admin)])


def _provider_read(provider: LLMProviderConfig) -> ProviderRead:
    return ProviderRead(
        id=provider.id,
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        api_key_masked=llm_admin_service.masked_key_of(provider),
        enabled=provider.enabled,
        models=provider.models,
    )


async def _get_provider_or_404(session: AsyncSession, provider_id: uuid.UUID) -> LLMProviderConfig:
    provider = await llm_admin_service.get_provider(session, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROVIDER_NOT_FOUND")
    return provider


@router.get("/providers", response_model=list[ProviderRead])
async def list_providers(
    session: AsyncSession = Depends(get_session),
) -> list[ProviderRead]:
    providers = await llm_admin_service.list_providers(session)
    return [_provider_read(p) for p in providers]


@router.post("/providers", response_model=ProviderRead, status_code=status.HTTP_201_CREATED)
async def create_provider(
    data: ProviderCreate,
    session: AsyncSession = Depends(get_session),
) -> ProviderRead:
    try:
        provider = await llm_admin_service.create_provider(session, data)
    except IntegrityError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="PROVIDER_NAME_EXISTS") from e
    return _provider_read(provider)


@router.patch("/providers/{provider_id}", response_model=ProviderRead)
async def update_provider(
    provider_id: uuid.UUID,
    data: ProviderUpdate,
    session: AsyncSession = Depends(get_session),
) -> ProviderRead:
    provider = await _get_provider_or_404(session, provider_id)
    provider = await llm_admin_service.update_provider(session, provider, data)
    return _provider_read(provider)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    provider = await _get_provider_or_404(session, provider_id)
    await llm_admin_service.delete_provider(session, provider)


@router.get("/routes", response_model=list[RouteItem])
async def list_routes(session: AsyncSession = Depends(get_session)) -> list[RouteItem]:
    routes = await llm_admin_service.list_routes(session)
    return [RouteItem.model_validate(r, from_attributes=True) for r in routes]


@router.put("/routes", response_model=list[RouteItem])
async def replace_routes(
    items: list[RouteItem],
    session: AsyncSession = Depends(get_session),
) -> list[RouteItem]:
    try:
        routes = await llm_admin_service.replace_routes(session, items)
    except llm_admin_service.InvalidRouteError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return [RouteItem.model_validate(r, from_attributes=True) for r in routes]


@router.get("/usage", response_model=list[UsageRow])
async def usage_report(
    project_id: uuid.UUID | None = Query(default=None),
    user_id: uuid.UUID | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
) -> list[UsageRow]:
    rows = await llm_admin_service.usage_report(
        session, project_id=project_id, user_id=user_id, days=days
    )
    return [UsageRow(**row) for row in rows]
