"""fastapi-users 装配：JWT 登录 + 邀请码注册 + /users/me。"""

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin, exceptions
from fastapi_users.authentication import AuthenticationBackend, BearerTransport, JWTStrategy
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.models.user import User
from app.schemas.user import UserCreate, UserRead, UserUpdate

JWT_LIFETIME_SECONDS = 60 * 60 * 24  # 24h


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    def __init__(self, user_db: SQLAlchemyUserDatabase) -> None:
        super().__init__(user_db)
        secret = get_settings().secret_key
        self.reset_password_token_secret = secret
        self.verification_token_secret = secret

    async def on_after_register(self, user: User, request: Request | None = None) -> None:
        """首个注册用户自动提升为平台 admin（实验室自举）。"""
        session: AsyncSession = self.user_db.session  # type: ignore[attr-defined]
        total = (await session.execute(select(func.count(User.id)))).scalar_one()
        if total == 1 and user.role != "admin":
            await self.user_db.update(user, {"role": "admin"})


async def get_user_db(
    session: AsyncSession = Depends(get_session),
) -> AsyncIterator[SQLAlchemyUserDatabase]:
    yield SQLAlchemyUserDatabase(session, User)


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncIterator[UserManager]:
    yield UserManager(user_db)


bearer_transport = BearerTransport(tokenUrl="api/auth/jwt/login")


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=get_settings().secret_key, lifetime_seconds=JWT_LIFETIME_SECONDS)


auth_backend = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])

# 其他路由用这个依赖拿当前登录用户
current_active_user = fastapi_users.current_user(active=True)


async def require_admin(user: User = Depends(current_active_user)) -> User:
    """管理端依赖：role=admin 才放行。"""
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="ADMIN_REQUIRED")
    return user


router = APIRouter()
router.include_router(
    fastapi_users.get_auth_router(auth_backend), prefix="/auth/jwt", tags=["auth"]
)


@router.post(
    "/auth/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    tags=["auth"],
)
async def register(
    request: Request,
    user_create: UserCreate,
    user_manager: UserManager = Depends(get_user_manager),
) -> User:
    """注册（实验室邀请制）：body 里的 invite_code 必须与 settings.INVITE_CODE 一致。"""
    if user_create.invite_code != get_settings().invite_code:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="INVALID_INVITE_CODE")
    try:
        return await user_manager.create(user_create, safe=True, request=request)
    except exceptions.UserAlreadyExists as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="REGISTER_USER_ALREADY_EXISTS"
        ) from e
    except exceptions.InvalidPasswordException as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "REGISTER_INVALID_PASSWORD", "reason": e.reason},
        ) from e


router.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate), prefix="/users", tags=["users"]
)
