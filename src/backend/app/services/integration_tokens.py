"""Issue, authenticate, list, and revoke external integration tokens."""

import hashlib
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.integration_token import IntegrationToken
from app.models.user import User
from app.schemas.integration_token import IntegrationTokenCreate

TOKEN_MARKER = "polaris_it_"
LAST_USED_WRITE_INTERVAL = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class AuthenticatedIntegrationToken:
    """The authenticated database token and its active user."""

    token: IntegrationToken
    user: User


def _digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def create_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    data: IntegrationTokenCreate,
) -> tuple[IntegrationToken, str]:
    """Create a high-entropy token and return its plaintext exactly once."""

    raw_token = TOKEN_MARKER + secrets.token_urlsafe(32)
    token = IntegrationToken(
        user_id=user_id,
        name=data.name,
        token_prefix=raw_token[:20],
        token_hash=_digest(raw_token),
        scopes=list(data.scopes),
        expires_at=utcnow() + timedelta(days=data.expires_in_days),
    )
    session.add(token)
    await session.commit()
    await session.refresh(token)
    return token, raw_token


async def list_tokens(session: AsyncSession, *, user_id: uuid.UUID) -> Sequence[IntegrationToken]:
    stmt = (
        select(IntegrationToken)
        .where(IntegrationToken.user_id == user_id)
        .order_by(IntegrationToken.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def get_owned_token(
    session: AsyncSession, *, token_id: uuid.UUID, user_id: uuid.UUID
) -> IntegrationToken | None:
    token = await session.get(IntegrationToken, token_id)
    if token is None or token.user_id != user_id:
        return None
    return token


async def revoke_token(session: AsyncSession, token: IntegrationToken) -> None:
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        await session.commit()


async def authenticate_token(
    session: AsyncSession, raw_token: str, *, touch: bool = True
) -> AuthenticatedIntegrationToken | None:
    """Resolve an active token without ever loading tokens by their public prefix."""

    if not raw_token.startswith(TOKEN_MARKER):
        return None
    token = await session.scalar(
        select(IntegrationToken).where(IntegrationToken.token_hash == _digest(raw_token))
    )
    now = utcnow()
    if token is None or token.revoked_at is not None or _as_utc(token.expires_at) <= now:
        return None
    user = await session.get(User, token.user_id)
    if user is None or not user.is_active:
        return None

    if touch and (
        token.last_used_at is None or now - _as_utc(token.last_used_at) >= LAST_USED_WRITE_INTERVAL
    ):
        token.last_used_at = now
        await session.commit()
    return AuthenticatedIntegrationToken(token=token, user=user)
