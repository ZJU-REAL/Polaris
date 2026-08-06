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
from app.schemas.project import ProjectCreate
from app.schemas.user import (
    ResetPasswordRequest,
    SendCodeRequest,
    SendCodeResult,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.services import email as email_service
from app.services import projects as projects_service
from app.services import registration_codes as codes_service
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

# 写入闸门放行的方法。收口在 HTTP 方法上，而不是给 211 个写端点逐个加守卫：
# app/api 下没有任何 GET/HEAD 处理器写库，也没有任何一个走 LLM 守卫（加这层时
# 遍历过全部处理器的 AST 核对），所以在这个代码库里「不安全方法」就等于「写」。
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 登出是 POST，但它只销毁调用者自己的会话。不放行的话游客连退出登录都做不到。
_READ_ONLY_ALLOWED_PATHS = frozenset({"/api/auth/jwt/logout"})


async def block_read_only_writes(
    request: Request,
    user: User | None = Depends(current_user_optional),
) -> None:
    """只读账号的写入闸门（挂在整个 /api 与 /mcp 上，见 main.py）。

    未登录直接放行——该拦的由各端点自己的鉴权依赖去拦，这里只管「登录了但只读」。
    """
    if user is None or not user.read_only:
        return
    if request.method in _SAFE_METHODS or request.url.path in _READ_ONLY_ALLOWED_PATHS:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="READ_ONLY_ACCOUNT")


async def require_admin(user: User = Depends(current_active_user)) -> User:
    """管理端依赖：role=admin 才放行。"""
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="ADMIN_REQUIRED")
    return user


async def block_read_only_personal_data(user: User = Depends(current_active_user)) -> User:
    """名册依赖：只读账号（游客）看不到实验室成员的个人信息。

    游客号是 role=admin + read_only，为的是「能看到管理端长什么样」，不是「能看到
    这个实验室里都有谁、邮箱是多少」。挡在接口上而不是只把前端标签藏起来——藏了
    标签接口还照答，等于没藏。
    """
    if user.read_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="READ_ONLY_NO_PERSONAL_DATA")
    return user


async def block_read_only_secrets(user: User = Depends(current_active_user)) -> User:
    """密钥依赖：只读账号（游客）看不到能拿去用的东西。

    与 block_read_only_personal_data 分开是因为理由不同：那条挡的是「实验室里有谁」，
    这条挡的是「拿了就能用」。注册码尤其如此——只读挡得住写，挡不住它被抄走，因为
    用它开号走的是公开的注册接口，写入闸门根本看不到那一步。
    """
    if user.read_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="READ_ONLY_NO_SECRETS")
    return user


async def _check_llm_quota(session: AsyncSession, user: User) -> None:
    if user.token_quota is not None:
        from app.services.users import tokens_used_by_user

        if await tokens_used_by_user(session, user.id) >= user.token_quota:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="TOKEN_QUOTA_EXCEEDED")


def _check_llm_access(user: User, *, chat: bool) -> None:
    """大模型使用权限：full=不限 | chat_only=仅文献对话与 AI 伴读 | blocked=锁定。"""
    # 只读账号一个 token 也不该花。写入闸门已经挡住了所有调模型的入口（它们全是
    # POST），这里再挡一次是为了不依赖「建号的人记得把 llm_access 设成 blocked」。
    if user.read_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="LLM_ACCESS_BLOCKED")
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
    注册码带预设研究方向时，注册成功后自动为新用户建好对应方向的项目。
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

    redeemed = await codes_service.redeem_code(session, user_create.invite_code)
    if redeemed is None and user_create.invite_code != get_settings().invite_code:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="INVALID_INVITE_CODE")
    preset_directions = [
        d for d in (redeemed.preset_directions or []) if isinstance(d, str) and d.strip()
    ] if redeemed else []
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
    # 邀请码预设的研究方向 → 自动建项目（只落一句话 statement，后续可补全）。
    # 建项目失败不影响注册结果。
    for direction in preset_directions:
        try:
            await projects_service.create_project(
                session,
                owner_id=user.id,
                data=ProjectCreate(name=direction[:255], statement=direction),
            )
        except Exception:  # noqa: BLE001
            logger.exception("preset direction project creation failed: %s", direction)
    return user


@router.get("/auth/capabilities", tags=["auth"])
async def auth_capabilities() -> dict[str, bool]:
    """前端据此决定是否显示验证码输入与「忘记密码」——没配 SMTP 就别给走不通的入口。"""
    enabled = get_settings().email_enabled
    return {"email": enabled, "password_reset": enabled, "register_email_code": enabled}


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
