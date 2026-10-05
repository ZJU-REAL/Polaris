"""Small, explicit retry boundary for transient SQLite writer contention."""

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import OperationalError


def is_sqlite_busy(exc: OperationalError) -> bool:
    detail = str(exc).lower()
    return any(
        marker in detail
        for marker in (
            "database is locked",
            "database table is locked",
            "database is busy",
            "sqlite_busy",
            "sqlite_locked",
        )
    )


async def retry_sqlite_busy[T](
    operation: Callable[[], Awaitable[T]],
    rollback: Callable[[], Awaitable[None]],
    *,
    attempts: int = 3,
    base_delay: float = 0.2,
) -> T:
    """Retry a complete transaction only for SQLite BUSY/LOCKED failures.

    The caller supplies the rollback callback so every retry starts from a usable SQLAlchemy
    session. Other database failures propagate immediately.
    """
    for attempt in range(attempts):
        try:
            return await operation()
        except OperationalError as exc:
            await rollback()
            if not is_sqlite_busy(exc) or attempt + 1 >= attempts:
                raise
            await asyncio.sleep(base_delay * (2**attempt))
    raise AssertionError("SQLite retry loop did not return or raise")
