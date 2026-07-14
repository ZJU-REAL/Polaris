"""文献 API 的 Redis 缓存（TTL 24h）与令牌桶限流。

- 缓存 key 含方法名与参数 hash；值为 JSON。
- Redis 不可用时静默降级为直连（不缓存），保证离线测试/本地无 redis 可跑。
"""

import asyncio
import hashlib
import json
import time
from typing import Any

from redis.asyncio import Redis

CACHE_TTL_SECONDS = 24 * 3600


def cache_key(namespace: str, method: str, params: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(params, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:32]
    return f"lit:{namespace}:{method}:{digest}"


class ResponseCache:
    """薄封装：get/set JSON，redis 异常一律吞掉（降级为不缓存）。"""

    def __init__(self, redis: Redis | None = None, ttl: int = CACHE_TTL_SECONDS) -> None:
        self._redis = redis
        self._ttl = ttl

    def _client(self) -> Redis | None:
        if self._redis is not None:
            return self._redis
        try:
            from app.core.redis import get_redis

            return get_redis()
        except Exception:  # noqa: BLE001 — 缓存是尽力而为
            return None

    async def get(self, key: str) -> Any | None:
        client = self._client()
        if client is None:
            return None
        try:
            raw = await client.get(key)
        except Exception:  # noqa: BLE001
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None

    async def set(self, key: str, value: Any) -> None:
        client = self._client()
        if client is None:
            return
        try:
            await client.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=self._ttl)
        except Exception:  # noqa: BLE001
            return


class TokenBucket:
    """进程内令牌桶：rate 个/秒、容量 capacity；取不到令牌时 async 等待。"""

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                refill = (now - self._updated) * self._rate
                self._tokens = min(self._capacity, self._tokens + refill)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait)


class MinIntervalLimiter:
    """最小请求间隔（arXiv 礼貌限速：默认 3s）。"""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._last + self._interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
