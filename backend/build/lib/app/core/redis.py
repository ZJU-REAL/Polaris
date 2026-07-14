"""Redis 异步客户端（懒初始化单例）。

API 进程与 worker 共用；测试通过 FastAPI 依赖覆盖 ``get_redis_dep``
（或直接向引擎注入 EventBus）替换为 fakeredis。
"""

from redis.asyncio import Redis

from app.core.config import get_settings

_client: Redis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def get_redis_dep() -> Redis:
    """FastAPI 依赖：SSE/WS 端点用；测试覆盖为 fakeredis。"""
    return get_redis()
