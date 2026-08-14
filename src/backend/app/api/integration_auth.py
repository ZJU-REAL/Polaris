"""Authentication shared by the MCP endpoint and integration APIs."""

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_user_optional
from app.core.db import get_session
from app.models.user import User
from app.services import integration_tokens as token_service

_bearer = HTTPBearer(auto_error=False)
_JWT_INTEGRATION_SCOPES = frozenset({"skills:read", "mcp:read"})


@dataclass(frozen=True, slots=True)
class IntegrationPrincipal:
    """An authenticated user plus the capabilities granted to this credential."""

    user: User
    scopes: frozenset[str]
    credential_kind: str


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="INVALID_INTEGRATION_TOKEN",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_integration_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    jwt_user: User | None = Depends(current_user_optional),
    session: AsyncSession = Depends(get_session),
) -> IntegrationPrincipal:
    """Accept a normal session JWT or a scoped Polaris integration token."""

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    raw_token = credentials.credentials
    if raw_token.startswith(token_service.TOKEN_MARKER):
        authenticated = await token_service.authenticate_token(
            session,
            raw_token,
            touch=request.method not in {"GET", "HEAD", "OPTIONS"},
        )
        if authenticated is None:
            raise _unauthorized()
        return IntegrationPrincipal(
            user=authenticated.user,
            scopes=frozenset(authenticated.token.scopes),
            credential_kind="integration_token",
        )
    if jwt_user is None:
        raise _unauthorized()
    return IntegrationPrincipal(
        user=jwt_user,
        scopes=_JWT_INTEGRATION_SCOPES,
        credential_kind="jwt",
    )


def require_integration_scope(scope: str):
    """Create a dependency that rejects credentials missing one required scope."""

    async def dependency(
        principal: IntegrationPrincipal = Depends(current_integration_principal),
    ) -> IntegrationPrincipal:
        if scope not in principal.scopes:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="INTEGRATION_SCOPE_REQUIRED")
        return principal

    return dependency


require_skills_read = require_integration_scope("skills:read")
require_mcp_read = require_integration_scope("mcp:read")
