"""fastapi-users 装配：JWT 登录 + 邀请码注册 + /users/me。"""

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin, exceptions
from fastapi_users.authentication import AuthenticationBackend, BearerTransport, JWTStrategy
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.models.user import User
from app.schemas.user import UserCreate, UserRead, UserUpdate
from app.services import registration_codes as codes_service

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

    async def authenticate(self, credentials: OAuth2PasswordRequestForm) -> User | None:
        """登录支持「邮箱或用户名」+ 密码：先按邮箱查，查不到再按用户名查。"""
        ident = credentials.username.strip()
        user: User | None = None
        try:
            user = await self.get_by_email(ident)
        except exceptions.UserNotExists:
            user = None
        if user is None and ident:
            session: AsyncSession = self.user_db.session  # type: ignore[attr-defined]
            user = (
                await session.execute(select(User).where(User.username == ident.lower()))
            ).scalar_one_or_none()
        if user is None:
            # 用户不存在也跑一次哈希，缓解时序攻击（对齐 fastapi-users 默认行为）
            self.password_helper.hash(credentials.password)
            return None
        verified, updated_hash = self.password_helper.verify_and_update(
            credentials.password, user.hashed_password
        )
        if not verified:
            return None
        if updated_hash is not None:
            await self.user_db.update(user, {"hashed_password": updated_hash})
        return user


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


async def _check_llm_quota(session: AsyncSession, user: User) -> None:
    if user.token_quota is not None:
        from app.services.users import tokens_used_by_user

        if await tokens_used_by_user(session, user.id) >= user.token_quota:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="TOKEN_QUOTA_EXCEEDED")


def _check_llm_access(user: User, *, chat: bool) -> None:
    """大模型使用权限：full=不限 | chat_only=仅文献对话与 AI 伴读 | blocked=锁定。"""
    level = user.llm_access or "full"
    if level == "blocked":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="LLM_ACCESS_BLOCKED")
    if level == "chat_only" and not chat:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="LLM_ACCESS_CHAT_ONLY")


def require_stage_access(feature: str):
    """阶段发起入口依赖：功能权限被禁用 → 403 FEATURE_DISABLED；
    大模型权限受限 → 403 LLM_ACCESS_*；用量达到配额 → 403 TOKEN_QUOTA_EXCEEDED。"""

    async def dep(
        user: User = Depends(current_active_user),
        session: AsyncSession = Depends(get_session),
    ) -> User:
        if not user.feature_enabled(feature):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="FEATURE_DISABLED")
        _check_llm_access(user, chat=False)
        await _check_llm_quota(session, user)
        return user

    return dep


# 各阶段守卫单例（ruff B008：避免在参数默认值中调用工厂）
require_forge = require_stage_access("forge")
require_review = require_stage_access("review")
require_experiment = require_stage_access("experiment")
require_writer = require_stage_access("writer")
require_paper_review = require_stage_access("paper_review")


async def require_llm_task(
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    """其他消耗大模型的入口（建库/精读编译/PPT/技能试运行等）：需 full 权限。"""
    _check_llm_access(user, chat=False)
    await _check_llm_quota(session, user)
    return user


async def require_llm_chat(
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    """文献对话 / AI 伴读入口：full 或 chat_only 均可。"""
    _check_llm_access(user, chat=True)
    await _check_llm_quota(session, user)
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
    session: AsyncSession = Depends(get_session),
) -> User:
    """注册（实验室邀请制）：body 里的 invite_code 需命中一个可用注册码。

    优先核销数据库里的管理注册码（可设过期 / 次数 / 停用）；未命中时回退到
    settings.invite_code 静态码（兜底，避免没建过码时无人能注册 / 把管理员锁死）。
    """
    redeemed = await codes_service.redeem_code(session, user_create.invite_code)
    if not redeemed and user_create.invite_code != get_settings().invite_code:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="INVALID_INVITE_CODE")
    # 用户名全局唯一（DB 也有唯一索引兜底，这里给出友好错误）
    taken = (
        await session.execute(select(User.id).where(User.username == user_create.username))
    ).first()
    if taken is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="USERNAME_TAKEN")
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
