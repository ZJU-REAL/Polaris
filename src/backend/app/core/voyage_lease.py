"""Cross-process ownership for one Voyage; duplicate deliveries are harmless."""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import timedelta

from sqlalchemy import or_, select, update

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.sqlite_retry import retry_sqlite_busy
from app.models.base import utcnow
from app.models.voyage import TERMINAL_STATUSES, VoyageRun

logger = logging.getLogger(__name__)

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 20
_execution_token: ContextVar[uuid.UUID | None] = ContextVar("voyage_execution_token", default=None)
_active_tokens: set[uuid.UUID] = set()


class VoyageLeaseLost(RuntimeError):
    pass


def current_execution_token() -> uuid.UUID | None:
    """The owner of the current engine/action context, never a database guess."""
    return _execution_token.get()


async def fence_execution(session, run_id) -> None:
    """Lock the ownership row before publishing a step result in this transaction."""
    token = _execution_token.get()
    if token is None:
        return  # Internal unit seams that do not drive a run.
    result = await session.execute(
        update(VoyageRun)
        .where(
            VoyageRun.id == run_id,
            VoyageRun.execution_token == token,
            VoyageRun.execution_expires_at > utcnow(),
        )
        .values(execution_token=token)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        with session.no_autoflush:
            owner = (await session.execute(select(
                VoyageRun.execution_token, VoyageRun.execution_expires_at, VoyageRun.status,
            ).where(VoyageRun.id == run_id))).one_or_none()
        logger.warning(
            "Voyage publication lease lost: run=%s exists=%s owner_matches=%s "
            "expires_at=%s status=%s now=%s",
            run_id, owner is not None, bool(owner and owner[0] == token),
            owner[1] if owner else None, owner[2] if owner else None, utcnow(),
        )
        await session.rollback()
        raise VoyageLeaseLost(str(run_id))


@asynccontextmanager
async def claim_execution(run_id, *, sessionmaker=None):
    factory = sessionmaker or get_sessionmaker()
    token = uuid.uuid4()

    async def change(kind):
        async with factory() as session:

            async def transaction():
                now = utcnow()
                stmt = update(VoyageRun).where(VoyageRun.id == run_id)
                if kind == "claim":
                    stmt = stmt.where(
                        VoyageRun.status.not_in(tuple(TERMINAL_STATUSES)),
                        or_(
                            VoyageRun.execution_token.is_(None),
                            VoyageRun.execution_expires_at.is_(None),
                            VoyageRun.execution_expires_at <= now,
                        ),
                    )
                else:
                    stmt = stmt.where(VoyageRun.execution_token == token)
                    if kind == "renew":
                        stmt = stmt.where(VoyageRun.execution_expires_at > now)
                stmt = stmt.values(
                    execution_token=None if kind == "release" else token,
                    execution_expires_at=(
                        None if kind == "release" else now + timedelta(seconds=LEASE_SECONDS)
                    ),
                )
                result = await session.execute(stmt)
                await session.commit()
                return result.rowcount == 1

            return await retry_sqlite_busy(transaction, session.rollback)

    if not await change("claim"):
        yield False
        return
    _active_tokens.add(token)
    owner = asyncio.current_task()
    context_token = _execution_token.set(token)

    async def heartbeat():
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                if not await change("renew"):
                    raise VoyageLeaseLost(str(run_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Voyage heartbeat failed: run=%s error=%s",
                           run_id, type(exc).__name__)
            if owner is not None:
                owner.cancel()

    task = asyncio.create_task(heartbeat(), name=f"voyage-lease:{run_id}")
    try:
        yield True
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        _execution_token.reset(context_token)
        _active_tokens.discard(token)
        await change("release")


async def reset_desktop_execution_leases(*, sessionmaker=None) -> int:
    """Clear previous-process owners before Desktop dispatch; never reclaim a server worker.

    Desktop runs one API process. Tokens currently held by this process are
    excluded, so calling startup recovery twice cannot evict its live tasks.
    This reset must precede enqueueing recovered jobs and opening HTTP service.
    """
    if not get_settings().is_desktop:
        raise RuntimeError("Voyage lease reset is restricted to Desktop startup")
    factory = sessionmaker or get_sessionmaker()
    async with factory() as session:

        async def transaction():
            statement = update(VoyageRun).where(
                or_(
                    VoyageRun.execution_token.is_not(None),
                    VoyageRun.execution_expires_at.is_not(None),
                )
            )
            if _active_tokens:
                statement = statement.where(
                    or_(
                        VoyageRun.execution_token.is_(None),
                        VoyageRun.execution_token.not_in(tuple(_active_tokens)),
                    )
                )
            result = await session.execute(
                statement.values(
                    execution_token=None,
                    execution_expires_at=None,
                )
            )
            await session.commit()
            return result.rowcount

        return await retry_sqlite_busy(transaction, session.rollback)
