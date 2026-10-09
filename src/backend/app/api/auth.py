"""会话装配：本地会话（/auth/local-session）签发 JWT + /users/me 资料。

Polaris 是单人、只在本机运行的产品（#842）：没有注册、密码登录、邮箱验证码与
找回密码。引擎第一次被访问时建出唯一的本地用户，渲染进程经
``POST /auth/local-session`` 取会话；其余路由照旧用 ``current_active_user`` 认 JWT。
"""

import secrets
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin
from fastapi_users.authentication import AuthenticationBackend, BearerTransport, JWTStrategy
from fastapi_users.password import PasswordHelper
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.models.user import User
from app.schemas.user import UserRead, UserUpdate
from app.services.local_user import LOCAL_USER_EMAIL


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    """只用来让 JWT 策略按 id 取回用户（HTTP 依赖与 WebSocket 握手共用）。"""

    def __init__(self, user_db: SQLAlchemyUserDatabase) -> None:
        super().__init__(user_db)
        secret = get_settings().secret_key
        self.reset_password_token_secret = secret
        self.verification_token_secret = secret


async def get_user_db(
    session: AsyncSession = Depends(get_session),
) -> AsyncIterator[SQLAlchemyUserDatabase]:
    yield SQLAlchemyUserDatabase(session, User)


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncIterator[UserManager]:
    yield UserManager(user_db)


bearer_transport = BearerTransport(tokenUrl="api/auth/local-session")


def get_jwt_strategy() -> JWTStrategy:
    settings = get_settings()
    return JWTStrategy(
        secret=settings.secret_key, lifetime_seconds=settings.session_lifetime_seconds
    )


auth_backend = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])

# 其他路由用这个依赖拿当前登录用户
current_active_user = fastapi_users.current_user(active=True)
# 没登录不报错、返回 None——写入闸门要挂在所有路由上，公开端点不能因此变成必须登录
current_user_optional = fastapi_users.current_user(active=True, optional=True)

router = APIRouter()


# 本机唯一用户的身份：固定邮箱 LOCAL_USER_EMAIL（定义在 services/local_user.py）做幂等键
# （stdio MCP 也按它找人，见 app/mcp/__main__.py）。
LOCAL_USERNAME = "local"


@router.post("/auth/local-session", tags=["auth"])
async def local_session(
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """幂等确保本地用户存在并直接签发会话——这是拿到会话的唯一方式。

    引擎只监听 127.0.0.1，能连上它的就是这台机器的主人。本地用户的密码是随机
    散列，没有任何端点会拿密码换会话。
    """
    user = (
        await session.execute(select(User).where(User.email == LOCAL_USER_EMAIL))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            email=LOCAL_USER_EMAIL,
            hashed_password=PasswordHelper().hash(secrets.token_urlsafe(32)),
            is_active=True,
            is_superuser=False,
            is_verified=True,
            display_name="Local",
            username=LOCAL_USERNAME,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    token = await get_jwt_strategy().write_token(user)
    return {"access_token": token, "token_type": "bearer"}


@router.get("/users/me", response_model=UserRead, tags=["users"])
async def read_me(user: User = Depends(current_active_user)) -> User:
    return user


@router.patch("/users/me", response_model=UserRead, tags=["users"])
async def update_me(
    body: UserUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> User:
    """改本人资料：目前只有显示名。"""
    if body.display_name is not None:
        name = body.display_name.strip()
        if not name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="EMPTY_DISPLAY_NAME")
        user.display_name = name
    await session.commit()
    await session.refresh(user)
    return user
