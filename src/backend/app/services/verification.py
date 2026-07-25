"""邮箱验证码：签发、校验、重发冷却与猜码限次。

注册验证与找回密码共用这套设施，靠 purpose 区分，互不通用（拿注册码去重置
密码不成立）。库里只存 HMAC 摘要；过期时间与冷却一律在 SQL 里比较，避开
sqlite 取回 naive datetime、与 aware 时间比较报错的老问题。
"""

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.email_code import EmailVerificationCode

logger = logging.getLogger(__name__)

CODE_TTL_SECONDS = 10 * 60
RESEND_COOLDOWN_SECONDS = 60
MAX_ATTEMPTS = 5

PURPOSE_REGISTER = "register"
PURPOSE_RESET = "reset"


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _digest(code: str) -> str:
    secret = get_settings().secret_key.encode()
    return hmac.new(secret, code.encode(), hashlib.sha256).hexdigest()


def _generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def cooldown_remaining(session: AsyncSession, email: str, purpose: str) -> int:
    """距离可以重发还剩几秒；0 = 现在就能发。"""
    now = datetime.now(UTC)
    since = now - timedelta(seconds=RESEND_COOLDOWN_SECONDS)
    last = (
        await session.execute(
            select(EmailVerificationCode.created_at)
            .where(
                EmailVerificationCode.email == email,
                EmailVerificationCode.purpose == purpose,
                EmailVerificationCode.created_at > since,
            )
            .order_by(EmailVerificationCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is None:
        return 0
    if last.tzinfo is None:  # sqlite 取回 naive，按 UTC 解读
        last = last.replace(tzinfo=UTC)
    remaining = RESEND_COOLDOWN_SECONDS - int((now - last).total_seconds())
    return max(0, remaining)


async def issue_code(session: AsyncSession, email: str, purpose: str) -> str:
    """签发新码并作废该邮箱同用途的旧码（旧码立刻失效，避免多码并存）。

    调用方须先用 cooldown_remaining 挡住高频重发。不 commit，交给上层事务。
    """
    now = datetime.now(UTC)
    await session.execute(
        delete(EmailVerificationCode).where(
            EmailVerificationCode.email == email,
            EmailVerificationCode.purpose == purpose,
        )
    )
    code = _generate_code()
    session.add(
        EmailVerificationCode(
            email=email,
            purpose=purpose,
            code_hash=_digest(code),
            expires_at=now + timedelta(seconds=CODE_TTL_SECONDS),
        )
    )
    await session.flush()
    return code


async def verify_code(session: AsyncSession, email: str, purpose: str, code: str) -> bool:
    """校验并消费验证码。错一次记一次，超过 MAX_ATTEMPTS 直接作废本轮码。"""
    now = datetime.now(UTC)
    row = (
        await session.execute(
            select(EmailVerificationCode)
            .where(
                EmailVerificationCode.email == email,
                EmailVerificationCode.purpose == purpose,
                EmailVerificationCode.consumed_at.is_(None),
                EmailVerificationCode.expires_at > now,
                EmailVerificationCode.attempts < MAX_ATTEMPTS,
            )
            .order_by(EmailVerificationCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return False

    # 先记一次尝试，再比对：即便后续抛错，这次猜测也已计入
    await session.execute(
        update(EmailVerificationCode)
        .where(EmailVerificationCode.id == row.id)
        .values(attempts=EmailVerificationCode.attempts + 1)
    )
    if not hmac.compare_digest(row.code_hash, _digest(code.strip())):
        return False

    await session.execute(
        update(EmailVerificationCode)
        .where(EmailVerificationCode.id == row.id)
        .values(consumed_at=now)
    )
    return True
