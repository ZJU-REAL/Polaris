"""异步数据库：engine / session 工厂、Declarative Base、FastAPI 依赖。

engine 懒初始化，便于测试通过环境变量覆盖 DATABASE_URL。
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _is_memory_sqlite(url: str) -> bool:
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    return parsed.get_backend_name() == "sqlite" and parsed.database in (None, "", ":memory:")


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict[str, object] = {"echo": False}
        if not _is_memory_sqlite(settings.database_url):
            # 文件型 sqlite 与 postgres 都是 QueuePool；只有内存库是 StaticPool，
            # 给它池参数会直接报错
            kwargs |= {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_timeout": settings.db_pool_timeout,
            }
            if not settings.is_sqlite:
                kwargs["pool_pre_ping"] = True  # 长跑连接可能被中间件掐掉，取用前探活一次
        _engine = create_async_engine(settings.database_url, **kwargs)
        if _engine.url.get_backend_name() == "sqlite":
            busy_timeout_ms = int(settings.sqlite_busy_timeout_ms)

            @event.listens_for(_engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _record):
                cursor = dbapi_connection.cursor()
                # 外键：sqlite 默认不强制，打开让 ON DELETE CASCADE 生效（对齐 postgres）
                cursor.execute("PRAGMA foreign_keys=ON")
                # WAL：读写不互斥——HTTP 请求读库不必等后台任务的写事务提交；
                # 持久化在库文件上，每条连接都设一次无害
                cursor.execute("PRAGMA journal_mode=WAL")
                # 写锁排队等，而不是立刻 "database is locked"
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                # WAL 下 NORMAL 不会损坏数据库，只可能在断电时丢最后几个事务
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()

    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


# ---- 旁路写入：记账 / 调用日志 / 终端日志 ----
#
# 这些写入各开一个独立 session，而调用方经常**正握着一个没提交的写事务**去调 LLM
# （先 flush 了几行，再嵌入/抽取）。SQLite 同一时刻只有一个写者：旁路写入只能等调用方
# 提交，而调用方又在等旁路写入返回——互相等到 busy_timeout 才以 "database is locked"
# 收场，记账行丢掉，调用方白白卡住整个超时。所以先用很短的超时试一次；被锁住就转到
# 后台去写（调用方提交后锁自然放开），不让调用方等。

SIDE_WRITE_QUICK_TIMEOUT_MS = 200
_side_writes: set[asyncio.Task[None]] = set()


async def _set_busy_timeout(conn: AsyncConnection, ms: int) -> None:
    if conn.dialect.name == "sqlite":
        await conn.exec_driver_sql(f"PRAGMA busy_timeout={int(ms)}")
        await conn.commit()


async def side_write(write: Callable[[AsyncSession], Awaitable[None]], *, what: str) -> None:
    """尽力而为的独立写入：不阻塞调用方，失败只记 warning。"""
    normal_ms = int(get_settings().sqlite_busy_timeout_ms)
    try:
        # 钉住同一条连接：短超时只给这一次尝试用，用完原样调回再还给连接池
        async with get_engine().connect() as conn:
            await _set_busy_timeout(conn, SIDE_WRITE_QUICK_TIMEOUT_MS)
            try:
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    try:
                        await write(session)
                        await session.commit()
                        return
                    except OperationalError as exc:
                        if "locked" not in str(exc).lower():
                            raise
                        await session.rollback()
            finally:
                await conn.rollback()
                await _set_busy_timeout(conn, normal_ms)
    except Exception:  # noqa: BLE001 — 旁路写入绝不影响主流程
        logger.warning("%s write failed", what, exc_info=True)
        return
    task = asyncio.create_task(_deferred_side_write(write, what), name=f"side-write:{what}")
    _side_writes.add(task)
    task.add_done_callback(_side_writes.discard)


async def _deferred_side_write(write: Callable[[AsyncSession], Awaitable[None]], what: str) -> None:
    try:
        async with get_sessionmaker()() as session:
            await write(session)
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("deferred %s write failed", what, exc_info=True)


async def drain_side_writes(timeout_s: float = 5.0) -> None:
    """等转到后台的旁路写入落盘（停机前、测试里用）。"""
    pending = [t for t in _side_writes if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout_s)


async def dispose_engine() -> None:
    """关闭 engine 并重置（测试/应用关闭时用）。"""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def create_all() -> None:
    """建表（仅 sqlite dev 环境在 startup 调用；postgres 走 alembic）。"""
    import app.models  # noqa: F401  确保所有模型注册到 metadata

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个 AsyncSession。"""
    async with get_sessionmaker()() as session:
        yield session
