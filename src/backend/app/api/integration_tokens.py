"""User-managed credentials for external agents and automation."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.schemas.integration_token import (
    IntegrationTokenCreate,
    IntegrationTokenCreated,
    IntegrationTokenRead,
)
from app.services import integration_tokens as token_service

router = APIRouter(prefix="/integration-tokens", tags=["integration-tokens"])


@router.get("", response_model=list[IntegrationTokenRead])
async def list_integration_tokens(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[IntegrationTokenRead]:
    tokens = await token_service.list_tokens(session, user_id=user.id)
    return [IntegrationTokenRead.model_validate(token) for token in tokens]


@router.post("", response_model=IntegrationTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_integration_token(
    data: IntegrationTokenCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> IntegrationTokenCreated:
    token, raw_token = await token_service.create_token(session, user_id=user.id, data=data)
    return IntegrationTokenCreated(
        **IntegrationTokenRead.model_validate(token).model_dump(), token=raw_token
    )


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_integration_token(
    token_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    token = await token_service.get_owned_token(session, token_id=token_id, user_id=user.id)
    if token is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="INTEGRATION_TOKEN_NOT_FOUND")
    await token_service.revoke_token(session, token)
