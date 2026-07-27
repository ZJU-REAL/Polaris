"""文献 API 的 Redis 缓存（TTL 24h）与令牌桶限流。

- 缓存 key 含方法名与参数 hash；值为 JSON。
- Redis 不可用时静默降级为直连（不缓存），保证离线测试/本地无 redis 可跑。
"""

import asyncio
import contextlib
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
    """最小请求间隔（arXiv 礼貌限速：默认 3s），**跨进程**。

    进程内的间隔是不够的：api 与 worker 是两个容器，各持一个限流器，于是 arXiv 看到的
    实际速率是设定值的两倍——用户手点「取 PDF」的同时 worker 正在跑建库，谁也不知道
    对方存在。所以闸门放在 Redis 上：``SET key NX PX interval`` 抢到才放行，抢不到就按
    剩余 TTL 等一等再抢。

    Redis 不可用时**降级为进程内间隔**（与 :class:`ResponseCache` 同一套优雅降级约定）：
    降级后限速偏松，但不会因为缓存层故障就阻断文献抓取。
    """

    #: 抢闸门的最多轮次；每轮至多等一个 interval，够用且不会无限自旋
    _MAX_GATE_ROUNDS = 40

    def __init__(
        self, interval: float, *, redis: Redis | None = None, key: str = "lit:arxiv:gate"
    ) -> None:
        self._interval = interval
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._redis = redis
        self._key = key

    def _client(self) -> Redis | None:
        if self._redis is not None:
            return self._redis
        try:
            from app.core.redis import get_redis

            return get_redis()
        except Exception:  # noqa: BLE001 — 拿不到 redis 就降级为进程内
            return None

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        if await self._acquire_shared():
            return
        await self._acquire_local()

    async def _acquire_shared(self) -> bool:
        """抢 Redis 闸门；成功返回 True。Redis 不可用返回 False 交给进程内兜底。"""
        client = self._client()
        if client is None:
            return False
        px = max(1, int(self._interval * 1000))
        try:
            for _ in range(self._MAX_GATE_ROUNDS):
                if await client.set(self._key, "1", nx=True, px=px):
                    return True
                ttl = await client.pttl(self._key)
                # ttl<0 表示键刚过期/不存在，立刻重抢；否则等它到期
                await asyncio.sleep(max(10, ttl) / 1000 if ttl and ttl > 0 else 0.01)
        except Exception:  # noqa: BLE001 — redis 故障不能阻断抓取
            return False
        # 轮次耗尽（闸门被持续占用）：放行，让上层的重试退避去兜
        return True

    async def _acquire_local(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._last + self._interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def penalize(self, seconds: float) -> None:
        """被限流后让**所有共享这个闸门的调用方**一起等，而不只是撞上 429 的那个。

        只让自己 sleep 是不够的：别的协程（乃至另一个进程）照样会在 3 秒后接着打过去，
        等于没退避。所以两件事一起做——把 Redis 闸门的 TTL 顶到惩罚时长（跨进程生效），
        再推进程内的 ``_last``（Redis 不可用时的兜底）。
        interval<=0（测试）时是 no-op，保持限流器整体关闭的语义。
        """
        if self._interval <= 0 or seconds <= 0:
            return
        client = self._client()
        if client is not None:
            # redis 故障只影响跨进程那一半，进程内的惩罚照常生效
            with contextlib.suppress(Exception):
                await client.set(self._key, "1", px=max(1, int(seconds * 1000)))
        async with self._lock:
            self._last = max(self._last, time.monotonic() + seconds)
