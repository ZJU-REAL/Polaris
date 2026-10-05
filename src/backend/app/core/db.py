"""异步数据库：engine / session 工厂、Declarative Base、FastAPI 依赖。

engine 懒初始化，便于测试通过环境变量覆盖 DATABASE_URL。
"""

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict[str, object] = {"echo": False}
        if settings.is_sqlite:
            # Modern aiosqlite defaults file databases to a 5+10 connection pool.
            # Summary workers retain sessions during LLM calls, so 20 workers can
            # starve their own lease heartbeats and dispatcher. Keep file-backed
            # SQLite unpooled; in-memory databases must retain their StaticPool.
            if make_url(settings.database_url).database not in (None, "", ":memory:"):
                kwargs["poolclass"] = NullPool
        else:
            kwargs |= {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_timeout": settings.db_pool_timeout,
                # 长跑的 worker 连接可能被中间件掐掉，取用前探活一次
                "pool_pre_ping": True,
            }
        _engine = create_async_engine(settings.database_url, **kwargs)
        if _engine.url.get_backend_name() == "sqlite":
            if _engine.url.database not in (None, "", ":memory:"):
                # Set the persistent journal mode once, before serving requests.
                # Doing this on every NullPool connection can contend with writers.
                @event.listens_for(_engine.sync_engine, "first_connect")
                def _sqlite_wal(dbapi_connection, _record):
                    cursor = dbapi_connection.cursor()
                    try:
                        cursor.execute("PRAGMA busy_timeout=10000")
                        cursor.execute("PRAGMA journal_mode=WAL")
                        if cursor.fetchone()[0].lower() != "wal":
                            raise RuntimeError("SQLite WAL mode could not be enabled")
                    finally:
                        cursor.close()

            # sqlite 默认不强制外键：打开 pragma，让 ON DELETE CASCADE 生效（对齐 postgres）
            @event.listens_for(_engine.sync_engine, "connect")
            def _sqlite_fk_on(dbapi_connection, _record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA busy_timeout=10000")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


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
