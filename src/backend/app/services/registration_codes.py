"""注册码服务：生成 / 列表 / 停用 / 注册时核销。"""

import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registration_code import RegistrationCode

# 人类友好的码：POLARIS-XXXXXX（大写字母 + 数字，去掉易混的 0/O/1/I）
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _gen_code() -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(6))
    return f"POLARIS-{body}"


async def create_code(
    session: AsyncSession,
    *,
    created_by: uuid.UUID | None,
    note: str = "",
    expires_days: int | None = None,
    max_uses: int | None = None,
) -> RegistrationCode:
    # 极小概率撞码，重试几次
    for _ in range(5):
        code = _gen_code()
        exists = (
            await session.execute(select(RegistrationCode.id).where(RegistrationCode.code == code))
        ).first()
        if exists is None:
            break
    rc = RegistrationCode(
        code=code,
        note=note.strip()[:255],
        created_by=created_by,
        expires_at=(datetime.now(UTC) + timedelta(days=expires_days)) if expires_days else None,
        max_uses=max_uses,
    )
    session.add(rc)
    await session.commit()
    await session.refresh(rc)
    return rc


async def list_codes(session: AsyncSession) -> Sequence[RegistrationCode]:
    stmt = select(RegistrationCode).order_by(RegistrationCode.created_at.desc())
    return (await session.execute(stmt)).scalars().all()


def code_status(rc: RegistrationCode) -> str:
    """有效性状态：active | revoked | expired | exhausted。"""
    if rc.revoked:
        return "revoked"
    if rc.expires_at is not None:
        expires = rc.expires_at
        if expires.tzinfo is None:  # sqlite 存储丢 tz
            expires = expires.replace(tzinfo=UTC)
        if expires < datetime.now(UTC):
            return "expired"
    if rc.max_uses is not None and rc.used_count >= rc.max_uses:
        return "exhausted"
    return "active"


async def revoke_code(session: AsyncSession, code_id: uuid.UUID) -> bool:
    rc = await session.get(RegistrationCode, code_id)
    if rc is None:
        return False
    rc.revoked = True
    await session.commit()
    return True


async def redeem_code(session: AsyncSession, code: str) -> bool:
    """注册时核销：命中一个可用注册码并原子自增 used_count 则返回 True。

    用带条件的 UPDATE 保证并发下不会超过 max_uses。不 commit（交给注册事务）。
    """
    now = datetime.now(UTC)
    stmt = (
        update(RegistrationCode)
        .where(
            RegistrationCode.code == code,
            RegistrationCode.revoked.is_(False),
            (RegistrationCode.expires_at.is_(None)) | (RegistrationCode.expires_at > now),
            (RegistrationCode.max_uses.is_(None))
            | (RegistrationCode.used_count < RegistrationCode.max_uses),
        )
        .values(used_count=RegistrationCode.used_count + 1)
    )
    result = await session.execute(stmt)
    return (result.rowcount or 0) > 0
