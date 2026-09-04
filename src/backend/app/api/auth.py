"""fastapi-users 装配：JWT 登录 + 邀请码注册 + /users/me。"""

import logging
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
from app.schemas.user import (
    ResetPasswordRequest,
    SendCodeRequest,
    SendCodeResult,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.services import email as email_service
from app.services import verification

logger = logging.getLogger(__name__)

# 有效期改为可配置（POLARIS_SESSION_LIFETIME_SECONDS），默认 30 天。
# 保留这个名字是为了不惊动现有 import；取值走 settings。
MIN_PASSWORD_LENGTH = 8


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    def __init__(self, user_db: SQLAlchemyUserDatabase) -> None:
        super().__init__(user_db)
        settings = get_settings()
        secret = settings.secret_key
        self.reset_password_token_secret = secret
        self.verification_token_secret = secret

    async def validate_password(self, password: str, user: UserCreate | User) -> None:
        """密码强度下限：≥8 位且同时含字母和数字（与前端强度条的「达标线」一致）。
        注册与重置密码两条路径都会过这里。"""
        if len(password) < MIN_PASSWORD_LENGTH:
            raise exceptions.InvalidPasswordException(reason="PASSWORD_TOO_SHORT")
        if not (any(c.isalpha() for c in password) and any(c.isdigit() for c in password)):
            raise exceptions.InvalidPasswordException(reason="PASSWORD_NEEDS_LETTER_AND_DIGIT")


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
    """注册（邀请制）：body 里的 invite_code 需等于 settings.invite_code 静态码。

    数据库注册码系统（可设过期/次数/预设方向）已随个人化定位移除（#585），
    只剩这一个部署级静态码把关。
    """
    # 邮箱验证码：开启邮件系统后必填必对（未配 SMTP 的部署跳过，否则没人能注册）
    if get_settings().email_enabled:
        verified = await verification.verify_code(
            session,
            verification.normalize_email(user_create.email),
            verification.PURPOSE_REGISTER,
            user_create.email_code,
        )
        if not verified:
            await session.commit()  # 保留 attempts 自增
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="INVALID_CODE")

    if user_create.invite_code != get_settings().invite_code:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="INVALID_INVITE_CODE")
    # 用户名全局唯一（DB 也有唯一索引兜底，这里给出友好错误）
    taken = (
        await session.execute(select(User.id).where(User.username == user_create.username))
    ).first()
    if taken is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="USERNAME_TAKEN")
    try:
        user = await user_manager.create(user_create, safe=True, request=request)
    except exceptions.UserAlreadyExists as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="REGISTER_USER_ALREADY_EXISTS"
        ) from e
    except exceptions.InvalidPasswordException as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "REGISTER_INVALID_PASSWORD", "reason": e.reason},
        ) from e
    return user


@router.get("/auth/capabilities", tags=["auth"])
async def auth_capabilities() -> dict[str, bool]:
    """前端据此决定是否显示验证码输入与「忘记密码」——没配 SMTP 就别给走不通的入口。

    ``local_session``：desktop 档位免登录（见 /auth/local-session）。前端见到它为
    true 就自动取会话、不再渲染登录页。"""
    settings = get_settings()
    enabled = settings.email_enabled
    return {
        "email": enabled,
        "password_reset": enabled,
        "register_email_code": enabled,
        "local_session": settings.is_desktop,
    }


# desktop 档位的本地用户身份。固定邮箱做幂等键；机器的主人就是自己的管理员。
LOCAL_USER_EMAIL = "local@polaris.desktop"
LOCAL_USERNAME = "local"


@router.post("/auth/local-session", tags=["auth"])
async def local_session(
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """desktop 档位免登录：幂等确保本地用户存在并直接签发会话。

    server 档位下这个端点结构性不存在（404）——不是 403：多用户部署里它就不该
    被发现。本地用户的密码是随机散列、永远无法用于登录——会话只能经这个端点
    取得，而它只在单机档位存在。
    """
    if not get_settings().is_desktop:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    user = (
        await session.execute(select(User).where(User.email == LOCAL_USER_EMAIL))
    ).scalar_one_or_none()
    if user is None:
        import secrets as _secrets

        from fastapi_users.password import PasswordHelper

        user = User(
            email=LOCAL_USER_EMAIL,
            hashed_password=PasswordHelper().hash(_secrets.token_urlsafe(32)),
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


@router.post("/auth/send-code", response_model=SendCodeResult, tags=["auth"])
async def send_code(
    payload: SendCodeRequest,
    session: AsyncSession = Depends(get_session),
) -> SendCodeResult:
    """发送邮箱验证码。

    purpose=register：邮箱已注册直接报错（注册接口本来也会告知，不存在额外泄露）。
    purpose=reset：无论邮箱是否存在都回 sent=true，只在存在时真发信——否则这就成了
    账号枚举接口。
    """
    if not get_settings().email_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="EMAIL_NOT_CONFIGURED")

    email = verification.normalize_email(payload.email)
    exists = (
        await session.execute(select(User.id).where(func.lower(User.email) == email))
    ).first() is not None

    if payload.purpose == verification.PURPOSE_REGISTER and exists:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="REGISTER_USER_ALREADY_EXISTS")

    remaining = await verification.cooldown_remaining(session, email, payload.purpose)
    if remaining > 0:
        return SendCodeResult(sent=False, retry_after=remaining)

    # 邮箱不存在时也照常签发（只是不发信）：否则「第二次请求有没有冷却」本身就能
    # 区分邮箱是否注册过，等于留了个账号枚举的旁路。
    code = await verification.issue_code(session, email, payload.purpose)
    await session.commit()
    if exists or payload.purpose == verification.PURPOSE_REGISTER:
        await email_service.send_verification_code(
            email, payload.purpose, code, verification.CODE_TTL_SECONDS // 60
        )
    return SendCodeResult(sent=True)


@router.post("/auth/reset-password", tags=["auth"])
async def reset_password(
    payload: ResetPasswordRequest,
    user_manager: UserManager = Depends(get_user_manager),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """凭邮箱验证码重设密码。验证码错误与邮箱不存在返回同一个错误码，避免枚举。"""
    if not get_settings().email_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="EMAIL_NOT_CONFIGURED")

    email = verification.normalize_email(payload.email)
    ok = await verification.verify_code(session, email, verification.PURPOSE_RESET, payload.code)
    if not ok:
        await session.commit()  # 保留 attempts 自增
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="INVALID_CODE")

    user = (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if user is None:
        await session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="INVALID_CODE")

    try:
        await user_manager.validate_password(payload.password, user)
    except exceptions.InvalidPasswordException as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_PASSWORD", "reason": e.reason},
        ) from e

    user.hashed_password = user_manager.password_helper.hash(payload.password)
    await session.commit()
    logger.info("密码已重置：user_id=%s", user.id)
    return {"ok": True}


@router.get("/auth/username-available", tags=["auth"])
async def username_available(
    username: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """注册表单实时检查用户名是否可用（公开接口，只回可用与否，不泄露其他信息）。"""
    uname = username.strip().lower()
    taken = (
        await session.execute(select(User.id).where(User.username == uname))
    ).first()
    return {"available": taken is None}


router.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate), prefix="/users", tags=["users"]
)
