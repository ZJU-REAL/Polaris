"""进程内 redis 替身（懒初始化单例）。

引擎是单进程：API、内联任务队列、定时任务都在同一个进程里，pubsub / 缓存 / 事件流
全部走进程内的 fakeredis，机器上不需要 Redis 服务（#842 删掉了连外部 Redis 的分支）。
"""

from redis.asyncio import Redis

_client: Redis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        import fakeredis.aioredis

        _client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def get_redis_dep() -> Redis:
    """FastAPI 依赖：SSE/WS 端点用；测试覆盖为 fakeredis。"""
    return get_redis()
